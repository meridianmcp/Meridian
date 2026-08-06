"""CI preflight: verify the MCP SDK dependency contract for local tunnel
extension servers (meridian-docs, meridian-outputs) before their install/test
steps run.

Root cause (GitHub Actions run 31047597879, origin/dev commit 5390b65):
meridian-docs CI installed the committed extension manifest with
dependencies=["mcp>=1.0", "latex2mathml>=2.0"]; pip resolved mcp==2.0.0,
which removed `mcp.server.fastmcp` (both extensions' entry points import
it), so the job died with a ModuleNotFoundError deep inside pytest
collection instead of a clear, named, early failure. Sprint item
0ab8139f-c482-450b-825d-852711d12f30 closes that gap. This script:

  1. Reads the COMMITTED manifests directly (never "whatever pip happened
     to resolve") and asserts the declared `mcp` dependency's version
     specifier both excludes the mcp 2.x major and has a lower bound at or
     above 1.27 -- parsed as real version tuples, never a literal string
     compare, so a legitimate future bump (e.g. >=1.28,<2, or a
     hypothetical new 1.x ceiling) keeps passing without editing this
     script.
  2. Once an extension is actually installed in the job's Python
     environment, imports `mcp.server.fastmcp` and the extension's own
     server module, and fails loudly with an actionable message if either
     import is broken or `<module>.mcp` is not a real FastMCP instance.
  3. Reports the checked-out commit SHA and the exact manifest dependency
     strings on every run -- success or failure -- so a CI log answers
     "which SHA, which pin" without anyone digging through a pytest
     traceback.

Deliberately stdlib-only (tomllib/re/dataclasses/subprocess/pathlib) --
mirrors scripts/check_orphaned_refs.py's "no pixi env needed" convention so
this can run as an early, cheap CI step (or locally) with nothing beyond a
python3 >=3.11 interpreter.

Usage:
    python scripts/preflight_extension_dependencies.py manifest
    python scripts/preflight_extension_dependencies.py import
"""

from __future__ import annotations

import argparse
import importlib
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# (manifest path relative to repo root, extension dir name, importable server module)
EXTENSIONS: tuple[tuple[str, str, str], ...] = (
    ("extensions/meridian-docs/pyproject.toml", "meridian-docs", "meridian_docs.server"),
    ("extensions/meridian-outputs/pyproject.toml", "meridian-outputs", "meridian_outputs.server"),
)

DEPENDENCY_NAME = "mcp"
MIN_LOWER_BOUND: tuple[int, ...] = (1, 27)
V2_MAJOR: tuple[int, ...] = (2,)

_REQ_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*(?:\[[^\]]*\])?\s*(.*?)\s*$")
_CLAUSE_RE = re.compile(r"(>=|<=|==|!=|~=|>|<)\s*([0-9][0-9A-Za-z.\-*]*)")
_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*")


class PreflightError(RuntimeError):
    """A dependency-contract violation with an actionable message."""


# --------------------------------------------------------------------------
# Version / specifier parsing (stdlib-only, release-segment tuples -- these
# pins only ever use >=, >, <, <= with plain numeric releases, so full PEP 440
# machinery -- pre-releases, epochs, local versions -- is unneeded here).
# --------------------------------------------------------------------------


def _parse_release(version_str: str) -> tuple[int, ...]:
    match = _VERSION_RE.match(version_str)
    if not match:
        raise PreflightError(f"could not parse {version_str!r} as a version")
    return tuple(int(part) for part in match.group(0).split("."))


def _cmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    length = max(len(a), len(b))
    a = a + (0,) * (length - len(a))
    b = b + (0,) * (length - len(b))
    return (a > b) - (a < b)


def _clause_permits(version: tuple[int, ...], op: str, bound_str: str) -> bool:
    bound = _parse_release(bound_str)
    c = _cmp(version, bound)
    if op == ">=":
        return c >= 0
    if op == ">":
        return c > 0
    if op == "<=":
        return c <= 0
    if op == "<":
        return c < 0
    if op == "==":
        return c == 0
    if op == "!=":
        return c != 0
    if op == "~=":
        # Compatible release: bound <= version < next-major-of-bound-prefix.
        # Only used defensively here; none of the pins in this repo use ~=.
        return c >= 0
    raise PreflightError(f"unsupported specifier operator {op!r}")


@dataclass
class ParsedRequirement:
    name: str
    raw: str
    clauses: list[tuple[str, str]]

    def permits(self, version_str: str) -> bool:
        version = _parse_release(version_str)
        return all(_clause_permits(version, op, bound) for op, bound in self.clauses)


def parse_requirement(dep_string: str) -> ParsedRequirement:
    without_marker = dep_string.split(";")[0].strip()
    match = _REQ_RE.match(without_marker)
    if not match:
        raise PreflightError(f"could not parse dependency string {dep_string!r}")
    name = match.group(1).strip().lower()
    rest = match.group(2).strip()
    clauses = _CLAUSE_RE.findall(rest)
    if rest and not clauses:
        raise PreflightError(
            f"could not parse version specifier {rest!r} in dependency {dep_string!r}"
        )
    return ParsedRequirement(name=name, raw=dep_string.strip(), clauses=clauses)


def find_dependency(dependencies: list[str], name: str = DEPENDENCY_NAME) -> ParsedRequirement:
    for dep in dependencies:
        req = parse_requirement(dep)
        if req.name == name:
            return req
    raise PreflightError(
        f"no {name!r} dependency declared at all (dependencies={dependencies!r}); "
        f"the {name} package is required by this extension's entry point."
    )


def validate_mcp_specifier(req: ParsedRequirement) -> None:
    """Raise PreflightError with an actionable message unless the specifier
    both (a) excludes the mcp 2.x major -- which removed
    mcp.server.fastmcp -- and (b) declares an explicit lower bound at or
    above 1.27 (the first mcp 1.x release both extensions are verified
    against). Bounds are compared as real version tuples, never a literal
    string match, so a legitimate future bump (e.g. >=1.28,<2, or a
    hypothetical 1.x ceiling change) keeps passing without editing this
    script.
    """
    lower_bounds = [_parse_release(v) for op, v in req.clauses if op in (">=", ">")]
    if not lower_bounds:
        raise PreflightError(
            f"{req.raw!r} has no lower bound on 'mcp' -- add one (e.g. '>=1.27') "
            f"so an old/broken pre-1.27 mcp release can't be resolved either."
        )
    if max(lower_bounds, key=lambda t: t) < MIN_LOWER_BOUND:
        best = max(lower_bounds, key=lambda t: t)
        raise PreflightError(
            f"{req.raw!r} declares a lower bound of "
            f"{'.'.join(map(str, best))}, below the required minimum "
            f"{'.'.join(map(str, MIN_LOWER_BOUND))} -- both extensions' "
            f"server.py use mcp.server.fastmcp APIs only verified from mcp "
            f"{'.'.join(map(str, MIN_LOWER_BOUND))} onward."
        )

    upper_bounds = [(op, v) for op, v in req.clauses if op in ("<", "<=")]
    if not upper_bounds:
        raise PreflightError(
            f"{req.raw!r} has no upper bound on 'mcp' -- this is the exact "
            f"regression from CI run 31047597879: pip is free to resolve "
            f"mcp==2.0.0+, which removed mcp.server.fastmcp and crashes both "
            f"extensions on import. Add an upper bound below mcp 2.0 (e.g. '<2')."
        )

    # Directly probe the exact failure mode: does this specifier, evaluated
    # as real clauses, actually accept the version that broke CI?
    if req.permits("2.0.0"):
        raise PreflightError(
            f"{req.raw!r} does not exclude the mcp 2.x major -- mcp==2.0.0 "
            f"removed mcp.server.fastmcp (used by both extensions' "
            f"server.py) and is still satisfied by this specifier. Add an "
            f"upper bound below 2.0 (e.g. '<2')."
        )


# --------------------------------------------------------------------------
# Manifest check
# --------------------------------------------------------------------------


@dataclass
class ManifestCheck:
    manifest_path: str
    dependency_string: str | None
    ok: bool
    message: str


def read_dependencies(manifest_path: Path) -> list[str]:
    with manifest_path.open("rb") as handle:
        data = tomllib.load(handle)
    return list(data.get("project", {}).get("dependencies", []))


def check_manifest(manifest_rel_path: str) -> ManifestCheck:
    manifest_path = REPO_ROOT / manifest_rel_path
    if not manifest_path.is_file():
        return ManifestCheck(manifest_rel_path, None, False, f"manifest not found: {manifest_path}")
    try:
        deps = read_dependencies(manifest_path)
        req = find_dependency(deps)
        validate_mcp_specifier(req)
    except PreflightError as exc:
        return ManifestCheck(manifest_rel_path, None, False, str(exc))
    return ManifestCheck(manifest_rel_path, req.raw, True, "ok")


def check_all_manifests() -> list[ManifestCheck]:
    return [check_manifest(manifest_rel) for manifest_rel, _, _ in EXTENSIONS]


# --------------------------------------------------------------------------
# Import check (assumes the extension(s) are already installed in this
# interpreter -- this function does not install anything itself)
# --------------------------------------------------------------------------


@dataclass
class ImportCheck:
    module_name: str
    ok: bool
    message: str


def check_fastmcp_importable() -> ImportCheck:
    try:
        fastmcp_module = importlib.import_module("mcp.server.fastmcp")
    except ImportError as exc:
        return ImportCheck(
            "mcp.server.fastmcp",
            False,
            f"mcp.server.fastmcp did not import in this environment ({exc}). "
            f"This is the exact failure mode from CI run 31047597879: an "
            f"installed mcp major without FastMCP. Check that the resolved "
            f"'mcp' package satisfies mcp>=1.27,<2 (run `pip show mcp`).",
        )
    if not hasattr(fastmcp_module, "FastMCP"):
        return ImportCheck(
            "mcp.server.fastmcp",
            False,
            "mcp.server.fastmcp imported but has no FastMCP attribute -- "
            "the installed mcp package is not the expected v1.x API shape.",
        )
    return ImportCheck("mcp.server.fastmcp", True, "ok")


def check_extension_entry_point(extension_dir: str, module_name: str) -> ImportCheck:
    """Import an ALREADY-installed extension's server module (this assumes
    `pip install -e extensions/<extension_dir>` already ran in this job --
    it does not install anything itself) and confirm its module-level `mcp`
    object is a real mcp.server.fastmcp.FastMCP instance."""
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        return ImportCheck(
            module_name,
            False,
            f"{module_name} did not import ({exc}). Is extensions/"
            f"{extension_dir} installed in this environment "
            f"(pip install -e extensions/{extension_dir})?",
        )
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        return ImportCheck(
            module_name,
            False,
            f"{module_name} imported, but mcp.server.fastmcp did not "
            f"({exc}) -- an incompatible mcp major is installed.",
        )
    mcp_obj = getattr(module, "mcp", None)
    if not isinstance(mcp_obj, FastMCP):
        return ImportCheck(
            module_name,
            False,
            f"{module_name}.mcp is not a mcp.server.fastmcp.FastMCP instance "
            f"(got {type(mcp_obj)!r}).",
        )
    return ImportCheck(module_name, True, "ok")


# --------------------------------------------------------------------------
# SHA reporting
# --------------------------------------------------------------------------


def resolve_commit_sha() -> str:
    for var in ("GITHUB_SHA", "MERIDIAN_GIT_SHA"):
        sha = os.environ.get(var)
        if sha:
            return sha
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def run_manifest_mode(*, stream=None, error_stream=None) -> int:
    # NOTE: resolve sys.stdout/sys.stderr at CALL time, not as default-argument
    # values (which would bind once at function-definition/import time and
    # silently bypass test-time stdout/stderr redirection, e.g. pytest's
    # capsys fixture swapping sys.stdout for the duration of a test).
    stream = stream if stream is not None else sys.stdout
    error_stream = error_stream if error_stream is not None else sys.stderr
    sha = resolve_commit_sha()
    checks = check_all_manifests()
    print(f"[preflight] checked-out SHA: {sha}", file=stream)
    for check in checks:
        status = "OK" if check.ok else "FAIL"
        dep = check.dependency_string or "<unresolved>"
        print(f"[preflight] {status} {check.manifest_path}: {dep}", file=stream)
        if not check.ok:
            print(f"[preflight]   reason: {check.message}", file=stream)
    if all(c.ok for c in checks):
        print(
            "[preflight] manifest check PASSED -- all extension manifests "
            "pin a compatible mcp major.",
            file=stream,
        )
        return 0
    print(
        "[preflight] manifest check FAILED -- fix the dependency pin(s) "
        "above before the extension install/test steps run.",
        file=error_stream,
    )
    return 1


def run_import_mode(*, stream=None, error_stream=None) -> int:
    stream = stream if stream is not None else sys.stdout
    error_stream = error_stream if error_stream is not None else sys.stderr
    sha = resolve_commit_sha()
    print(f"[preflight] checked-out SHA: {sha}", file=stream)
    results = [check_fastmcp_importable()]
    for _, extension_dir, module_name in EXTENSIONS:
        results.append(check_extension_entry_point(extension_dir, module_name))
    ok = True
    for result in results:
        status = "OK" if result.ok else "FAIL"
        print(f"[preflight] {status} import {result.module_name}", file=stream)
        if not result.ok:
            ok = False
            print(f"[preflight]   reason: {result.message}", file=error_stream)
    if ok:
        print("[preflight] import check PASSED.", file=stream)
        return 0
    print("[preflight] import check FAILED.", file=error_stream)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=("manifest", "import"),
        help=(
            "'manifest' validates the committed pyproject.toml dependency "
            "pins with no install required; 'import' verifies "
            "mcp.server.fastmcp and each extension's entry point actually "
            "import in this (already-installed) environment."
        ),
    )
    args = parser.parse_args(argv)
    if args.mode == "manifest":
        return run_manifest_mode()
    return run_import_mode()


if __name__ == "__main__":
    raise SystemExit(main())
