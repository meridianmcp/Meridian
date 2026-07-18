"""Pre-merge AST check for orphaned local references.

REFILED (original 691d367b stuck, confirmed unfixed): the codebase graph's
CALLS-edge confidence scoring is too noisy to gate a merge on (confirmed
live) -- it flags legitimate dynamic dispatch as broken and misses real
breakage buried under high-confidence edges. This script replaces that
approach with a dedicated, deterministic AST name-resolution walk scoped to
first-party source only:

  1. Parse every first-party module under DEFAULT_TARGETS and record its
     top-level names (def/class/assignment/import, including names defined
     inside a top-level `if`/`try`/`with` for platform-conditional code).
  2. For every `from <local module> import <name>` -- including relative
     imports -- verify `<name>` is actually defined in the target module.
  3. For every `<local_alias>.<attr>` access where `<local_alias>` is bound
     to a first-party module via `import`/`from ... import`, verify `<attr>`
     is defined there too.

A rename or removal that leaves a stale import or a stale `module.attr`
reference dangling is exactly the class of bug F821 (single-file undefined-
name) cannot see, because pyflakes never resolves whether an imported name
actually exists in the *target* module. Modules with a `from x import *`
are skipped for that target (can't know what a star-import provides), which
keeps this check conservative rather than noisy.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGETS = ["meridian", "scripts"]
SKIP_DIR_NAMES = {"__pycache__", "node_modules", ".git"}
SELF_NAME = Path(__file__).name


@dataclass
class ModuleInfo:
    dotted: str
    path: Path
    tree: ast.Module | None
    is_package: bool = False
    exported: set[str] = field(default_factory=set)
    submodules: set[str] = field(default_factory=set)
    has_star_import: bool = False
    has_dynamic_exports: bool = False


@dataclass
class Finding:
    path: Path
    line_no: int
    kind: str  # "import" | "attribute"
    reference: str
    reason: str


def iter_source_files() -> list[Path]:
    files: set[Path] = set()
    for raw in DEFAULT_TARGETS:
        base = ROOT / raw
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if any(part in SKIP_DIR_NAMES for part in path.parts):
                continue
            files.add(path)
    return sorted(files)


def path_to_dotted(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _collect_assign_target(target: ast.expr, names: set[str]) -> None:
    if isinstance(target, ast.Name):
        names.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            _collect_assign_target(elt, names)


def collect_top_level_names(tree: ast.Module) -> tuple[set[str], bool]:
    """Top-level names defined by `tree`, including those nested inside a
    top-level if/try/with (common for platform-conditional definitions)."""
    names: set[str] = set()
    has_star = False

    def visit_body(body: list[ast.stmt]) -> None:
        nonlocal has_star
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    _collect_assign_target(target, names)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    names.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if any(alias.name == "*" for alias in node.names):
                    has_star = True
                for alias in node.names:
                    if alias.name != "*":
                        names.add(alias.asname or alias.name)
            elif isinstance(node, ast.If):
                visit_body(node.body)
                visit_body(node.orelse)
            elif isinstance(node, ast.Try):
                visit_body(node.body)
                for handler in node.handlers:
                    visit_body(handler.body)
                visit_body(node.orelse)
                visit_body(node.finalbody)
            elif isinstance(node, ast.With):
                visit_body(node.body)

    visit_body(tree.body)
    return names, has_star


def _has_dynamic_exports(tree: ast.Module) -> bool:
    """True if the module re-exports names dynamically at runtime in a way an
    AST walk cannot enumerate -- e.g. the compat-shim pattern

        globals().update({k: v for k, v in vars(_impl).items() ...})

    used by meridian/docs_intel.py and meridian/latex_intel.py (d45c2cc8) to
    re-export a relocated package's full namespace. Such modules must be
    treated as opaque (like a star-import target) rather than flagged for
    every name importers pull from them.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
            and isinstance(node.func.value, ast.Call)
            and isinstance(node.func.value.func, ast.Name)
            and node.func.value.func.id == "globals"
        ):
            return True
    return False


def build_module_index(files: list[Path]) -> dict[str, ModuleInfo]:
    index: dict[str, ModuleInfo] = {}
    for path in files:
        dotted = path_to_dotted(path)
        is_package = path.name == "__init__.py"
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            index[dotted] = ModuleInfo(dotted=dotted, path=path, tree=None, is_package=is_package)
            continue
        exported, has_star = collect_top_level_names(tree)
        index[dotted] = ModuleInfo(
            dotted=dotted,
            path=path,
            tree=tree,
            is_package=is_package,
            exported=exported,
            has_star_import=has_star,
            has_dynamic_exports=_has_dynamic_exports(tree),
        )
    for dotted in index:
        if "." not in dotted:
            continue
        parent, _, child = dotted.rpartition(".")
        if parent in index:
            index[parent].submodules.add(child)
    return index


def resolve_from_import(node: ast.ImportFrom, info: ModuleInfo) -> str | None:
    """Resolve a `from X import ...` (absolute or relative) to a dotted
    module path first-party to this repo, or None if unresolvable."""
    if node.level == 0:
        return node.module
    parts = info.dotted.split(".")
    # level=1 means "this module's own containing package"; if the current
    # module IS a package (__init__.py), that's itself, not its parent.
    cut = node.level - 1 if info.is_package else node.level
    if cut > len(parts):
        return None
    base = parts if cut == 0 else parts[:-cut]
    if node.module:
        base = base + node.module.split(".")
    return ".".join(base) if base else None


def check_file(path: Path, index: dict[str, ModuleInfo]) -> list[Finding]:
    info = index[path_to_dotted(path)]
    if info.tree is None:
        return []

    findings: list[Finding] = []
    local_alias_targets: dict[str, str] = {}

    for node in ast.walk(info.tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    local_alias_targets[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    local_alias_targets[top] = top
        elif isinstance(node, ast.ImportFrom):
            target_dotted = resolve_from_import(node, info)
            if not target_dotted or target_dotted not in index:
                continue  # third-party / stdlib / unresolvable -- not our concern
            target = index[target_dotted]
            if target.tree is None or target.has_star_import or target.has_dynamic_exports:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                local_name = alias.asname or alias.name
                if alias.name in target.exported or alias.name in target.submodules:
                    local_alias_targets[local_name] = f"{target_dotted}.{alias.name}"
                    continue
                findings.append(
                    Finding(
                        path=path,
                        line_no=node.lineno,
                        kind="import",
                        reference=f"from {target_dotted} import {alias.name}",
                        reason=f"'{alias.name}' is not defined in {target_dotted} "
                        "(renamed or removed?)",
                    )
                )

    for node in ast.walk(info.tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            target_dotted = local_alias_targets.get(node.value.id)
            if not target_dotted or target_dotted not in index:
                continue
            target = index[target_dotted]
            if target.tree is None or target.has_star_import or target.has_dynamic_exports:
                continue
            attr = node.attr
            if attr.startswith("__") or attr in target.exported or attr in target.submodules:
                continue
            findings.append(
                Finding(
                    path=path,
                    line_no=node.lineno,
                    kind="attribute",
                    reference=f"{node.value.id}.{attr}",
                    reason=f"'{attr}' is not defined in {target_dotted} "
                    "(renamed or removed?)",
                )
            )

    return findings


def main() -> int:
    files = [p for p in iter_source_files() if p.name != SELF_NAME]
    index = build_module_index(files + [Path(__file__).resolve()])

    findings: list[Finding] = []
    for path in files:
        findings.extend(check_file(path, index))

    findings.sort(key=lambda f: (str(f.path), f.line_no, f.reference))
    for f in findings:
        rel = f.path.relative_to(ROOT).as_posix()
        print(f"[{f.kind}] {rel}:{f.line_no} {f.reference} -- {f.reason}")

    print(f"\nSummary: {len(findings)} orphaned local reference(s) found "
          f"across {len(files)} first-party file(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
