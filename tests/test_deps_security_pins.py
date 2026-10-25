"""Regression coverage for the dependency pins that close Dependabot alerts.

Alerts 1-8, 37, 38, 54 and 55 stay open until every checked-in lock resolves
each affected package at or above its first patched version. Three locks are
involved:

- ``extensions/meridian-deep-research/requirements-lock.txt`` (pip pins),
- ``package-lock.json`` (npm, root lockfile), and
- ``pixi.lock`` (every pixi environment's pypi entries, one packages section).

Floors come from the ``first_patched_version`` each open alert reports in the
Dependabot API (repo meridianmcp/Meridian, read 2026-10-25). Where several
alerts name the same package, the floor is the highest first patched version
in that set, so one floor closes all of them.

The test only reads the checked-in files. It runs no pixi solve, no npm and
no network access, so it belongs in the normal pytest sweep.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[1]
REQUIREMENTS_LOCK = REPO_ROOT / "extensions" / "meridian-deep-research" / "requirements-lock.txt"
PACKAGE_LOCK = REPO_ROOT / "package-lock.json"
PIXI_LOCK = REPO_ROOT / "pixi.lock"

# pip packages. name -> (first patched version, alert numbers it closes).
PIP_FLOORS: dict[str, tuple[str, tuple[int, ...]]] = {
    # Alert 1 (first patched 48.0.1), alerts 5 and 6 (49.0.0), alert 2 (50.0.0).
    "cryptography": ("50.0.0", (1, 2, 5, 6)),
    # Alert 38 (critical, first patched 2026.10.19).
    "certifi": ("2026.10.19", (38,)),
    # Alerts 7 and 8 (first patched 4.0.0).
    "oauthlib": ("4.0.0", (7, 8)),
    # Alerts 3 and 4 (first patched 0.10.0).
    "langgraph-api": ("0.10.0", (3, 4)),
    # Alert 54 (first patched 1.45.1).
    "opentelemetry-api": ("1.45.1", (54,)),
}

# npm packages. name -> (first patched version, alert numbers it closes).
NPM_FLOORS: dict[str, tuple[str, tuple[int, ...]]] = {
    # Alert 37 (first patched 4.13.11).
    "hono": ("4.13.11", (37,)),
    # Alert 55 (first patched 10.7.4).
    "ip-address": ("10.7.4", (55,)),
}

# pip packages that must appear in pixi.lock. Absence would make the floor
# check pass vacuously, so these two are required, not merely checked if present.
PIXI_REQUIRED = frozenset({"certifi", "opentelemetry-api"})


def _normalize(name: str) -> str:
    """PEP 503 name normalization, so ``langgraph_api`` and ``langgraph-api`` compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _at_least(actual: str, floor: str) -> bool:
    """True when ``actual`` is greater than or equal to ``floor``.

    Only the leading numeric release segment is compared, so ``0.66b1`` reads as
    ``0.66``. Components are zero-padded so ``1.45`` and ``1.45.0`` compare equal.
    """
    match_actual = re.match(r"\d+(?:\.\d+)*", actual)
    match_floor = re.match(r"\d+(?:\.\d+)*", floor)
    assert match_actual and match_floor, f"unparseable version: actual={actual!r} floor={floor!r}"
    a = [int(part) for part in match_actual.group(0).split(".")]
    f = [int(part) for part in match_floor.group(0).split(".")]
    width = max(len(a), len(f))
    a += [0] * (width - len(a))
    f += [0] * (width - len(f))
    return a >= f


def _requirements_pins(path: Path) -> dict[str, list[str]]:
    pins: dict[str, list[str]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;,#]+)", raw.strip())
        if match:
            pins.setdefault(_normalize(match.group(1)), []).append(match.group(2))
    return pins


def _pixi_pypi_pins(path: Path) -> dict[str, list[str]]:
    """Collect every pypi entry's name and version from the pixi ``packages:`` section.

    Each environment references these shared entries, so checking the packages
    section covers every environment (default, dev, docs, semantic, deep-research).
    """
    pins: dict[str, list[str]] = {}
    in_packages = False
    kind: str | None = None
    name: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line == "packages:":
            in_packages = True
            continue
        if not in_packages:
            continue
        if line.startswith("- "):
            kind = line[2:].split(":", 1)[0]
            name = None
            continue
        if kind != "pypi":
            continue
        if line.startswith("  name: "):
            name = _normalize(line[len("  name: ") :].strip())
        elif line.startswith("  version: ") and name is not None:
            pins.setdefault(name, []).append(line[len("  version: ") :].strip())
            name = None
    return pins


def _npm_versions(path: Path) -> dict[str, list[str]]:
    """Collect every installed copy's version from the root lockfile, including nested copies."""
    data = json.loads(path.read_text(encoding="utf-8"))
    found: dict[str, list[str]] = {}
    for key, entry in data.get("packages", {}).items():
        if not key or "node_modules/" not in key:
            continue
        name = key.rsplit("node_modules/", 1)[1]
        version = entry.get("version")
        if version:
            found.setdefault(name, []).append(version)
    for name, entry in data.get("dependencies", {}).items():
        version = entry.get("version")
        if version:
            found.setdefault(name, []).append(version)
    return found


def test_lock_parsers_read_real_entries() -> None:
    """Guards against a parser that silently reads nothing and passes every floor."""
    assert len(_requirements_pins(REQUIREMENTS_LOCK)) > 100
    assert len(_pixi_pypi_pins(PIXI_LOCK)) > 100
    assert "hono" in _npm_versions(PACKAGE_LOCK)


@pytest.mark.parametrize("package", sorted(PIP_FLOORS))
def test_requirements_lock_pin_meets_first_patched_version(package: str) -> None:
    floor, alerts = PIP_FLOORS[package]
    pins = _requirements_pins(REQUIREMENTS_LOCK).get(_normalize(package), [])
    assert pins, f"{package} is missing from requirements-lock.txt (alerts {alerts})"
    for version in pins:
        assert _at_least(version, floor), (
            f"requirements-lock.txt pins {package}=={version}; alerts {alerts} need >= {floor}"
        )


@pytest.mark.parametrize("package", sorted(NPM_FLOORS))
def test_package_lock_pin_meets_first_patched_version(package: str) -> None:
    floor, alerts = NPM_FLOORS[package]
    versions = _npm_versions(PACKAGE_LOCK).get(package, [])
    assert versions, f"{package} is missing from package-lock.json (alerts {alerts})"
    for version in versions:
        assert _at_least(version, floor), (
            f"package-lock.json resolves {package}@{version}; alerts {alerts} need >= {floor}"
        )


@pytest.mark.parametrize("package", sorted(PIP_FLOORS))
def test_pixi_lock_pypi_entry_meets_first_patched_version(package: str) -> None:
    floor, alerts = PIP_FLOORS[package]
    versions = _pixi_pypi_pins(PIXI_LOCK).get(_normalize(package), [])
    if package in PIXI_REQUIRED:
        assert versions, f"{package} is missing from pixi.lock (alerts {alerts})"
    for version in versions:
        assert _at_least(version, floor), (
            f"pixi.lock resolves {package}=={version} in an environment; alerts {alerts} need >= {floor}"
        )
