#!/usr/bin/env python3
"""Boundary checker for the Meridian desktop app.

Rules:
  (a) Qt imports (PySide6, PySide2, PyQt5, PyQt6, PyQt4, shiboken6, shiboken2)
      appear only under meridian_desktop/ui/ and in the UI files listed in
      UI_FILES. tray.py is listed there on purpose.
  (a2) A Qt root may not be loaded by name at run time outside the same UI
      files either. Checked: __import__, importlib.import_module, the
      importlib.__import__ and builtins.__import__ forms, aliases bound by an
      import in the same file, relative names with a literal package, and
      eval or exec of a string literal that imports one. Only string literals
      are seen. A name built at run time (a variable, a join, getattr) is not
      checked. This catches slips; it is not a sandbox.
  (b) No file in the repo references the meridian-latex engine path. The
      patterns are assembled from pieces so this file does not match itself.

Exit 0 and print "boundary ok" when clean. Exit 1 and print one VIOLATION line
per problem otherwise. The scan root is injectable (--root) so tests can run
the checker on a temporary tree.
"""

from __future__ import annotations

import argparse
import ast
import os
import warnings
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE = "meridian_desktop"
UI_DIR = Path(PACKAGE) / "ui"
# tray.py is UI code and stays where it is. It is listed here so the exception is explicit.
UI_FILES = (Path(PACKAGE) / "tray.py",)
QT_ROOTS = ("PySide6", "PySide2", "PyQt5", "PyQt6", "PyQt4", "shiboken6", "shiboken2")
# Built from pieces so that this file does not contain the literal paths.
# The doubled-backslash forms are what a Windows path looks like inside a
# Python or JSON string literal, so they must match too.
FORBIDDEN_REFS = (
    "extensions/" + "meridian-latex",
    "extensions\\" + "meridian-latex",
    "extensions\\\\" + "meridian-latex",
    "meridian-latex/" + "engine",
    "meridian-latex\\" + "engine",
    "meridian-latex\\\\" + "engine",
)
# Generated or dependency trees. They are not repo content.
SKIP_DIRS = {
    ".git",
    ".claude",  # agent worktrees are full checkouts of this repo, scanned once per worktree
    ".pixi",
    ".venv",
    "venv",
    "build",
    "dist",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


# Dynamic imports (rule a2). IMPORT_MODULES are bound by default, not only by an import
# statement, because a string run by eval or exec can use them without importing them.
IMPORT_MODULES = ("builtins", "importlib")
# Dotted callables that import a module from a name string. The bare builtin
# __import__ is handled separately, since it has no module prefix.
DYNAMIC_IMPORTERS = {"builtins.__import__", "importlib.__import__", "importlib.import_module"}
# Builtins that run source text. A string literal passed to one is parsed and checked.
SOURCE_RUNNERS = ("eval", "exec")


def _is_ui_file(rel: Path) -> bool:
    return rel.parts[:2] == UI_DIR.parts or rel in UI_FILES


def _qt_imports(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.lineno, node.module))
    return [(line, name) for line, name in found if name.split(".")[0] in QT_ROOTS]


def _arg(call: ast.Call, index: int, keyword: str) -> ast.expr | None:
    """The positional argument at index, else the keyword argument named keyword."""
    if index < len(call.args):
        return call.args[index]
    return next((kw.value for kw in call.keywords if kw.arg == keyword), None)


def _str_arg(call: ast.Call, index: int, keyword: str) -> str | None:
    node = _arg(call, index, keyword)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _import_aliases(tree: ast.AST) -> tuple[dict[str, str], dict[str, str]]:
    """Map local names to IMPORT_MODULES (name -> module) and from-imported importers
    (name -> "module.function")."""
    modules = {name: name for name in IMPORT_MODULES}
    importers: dict[str, str] = {}
    for item in ast.walk(tree):
        if isinstance(item, ast.Import):
            for alias in item.names:
                if alias.name in IMPORT_MODULES:
                    modules[alias.asname or alias.name] = alias.name
        elif isinstance(item, ast.ImportFrom) and item.level == 0 and item.module in IMPORT_MODULES:
            for alias in item.names:
                dotted = f"{item.module}.{alias.name}"
                if dotted in DYNAMIC_IMPORTERS:
                    importers[alias.asname or alias.name] = dotted
    return modules, importers


def _importer_form(func: ast.expr, modules: dict[str, str], importers: dict[str, str]) -> str | None:
    """The importer a call target is: "__import__" for the builtin, a DYNAMIC_IMPORTERS
    name, or None when the target is neither."""
    if isinstance(func, ast.Name):
        return "__import__" if func.id == "__import__" else importers.get(func.id)
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        module = modules.get(func.value.id)
        if module is not None and f"{module}.{func.attr}" in DYNAMIC_IMPORTERS:
            return f"{module}.{func.attr}"
    return None


def _imported_name(call: ast.Call, form: str) -> str | None:
    """The name a dynamic import call loads, when its root is a Qt root, else None.

    A relative name (leading dot) is anchored by a literal package argument, which only
    import_module takes. __import__ with a literal level above zero resolves against the
    caller's own package, so it cannot load a Qt root.
    """
    name = _str_arg(call, 0, "name")
    if name is None:
        return None
    if name.startswith("."):
        package = _str_arg(call, 1, "package") if form == "importlib.import_module" else None
        if package is None or package.split(".")[0] not in QT_ROOTS:
            return None
        return f"{name} in package {package}"
    if form.endswith("__import__"):
        level = _arg(call, 4, "level")
        if isinstance(level, ast.Constant) and isinstance(level.value, int) and level.value > 0:
            return None
    return name if name.split(".")[0] in QT_ROOTS else None


def _source_imports(call: ast.Call, runner: str, source: str | bytes) -> list[tuple[int, str, str]]:
    """Qt imports in a string literal that eval or exec runs, reported at the runner's line."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            inner = ast.parse(source)
    except SyntaxError:
        return []  # the runner raises before it can import anything
    where = f"{runner}() of a string literal"
    found = [(call.lineno, name, where) for _, name in _qt_imports(inner)]
    for _, name, form in _dynamic_qt_imports(inner):
        found.append((call.lineno, name, f"{where}, calling {form}"))
    return found


def _dynamic_qt_imports(tree: ast.AST) -> list[tuple[int, str, str]]:
    """(line, name, form) for each dynamic import in tree that loads a Qt root by a literal
    name, and for each eval or exec of a literal string that imports one."""
    modules, importers = _import_aliases(tree)
    found: list[tuple[int, str, str]] = []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        if isinstance(call.func, ast.Name) and call.func.id in SOURCE_RUNNERS:
            source = call.args[0] if call.args else None
            if isinstance(source, ast.Constant) and isinstance(source.value, (str, bytes)):
                found.extend(_source_imports(call, call.func.id, source.value))
            continue
        form = _importer_form(call.func, modules, importers)
        if form is None:
            continue
        name = _imported_name(call, form)
        if name is not None:
            found.append((call.lineno, name, form))
    return found


def _repo_files(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            yield Path(dirpath) / name


def check(root: Path) -> list[str]:
    root = Path(root)
    package = root / PACKAGE
    if not package.is_dir():
        return [f"{PACKAGE}/ not found under {root} (fail closed)"]

    problems: list[str] = []
    for path in sorted(package.rglob("*.py")):
        rel = path.relative_to(root)
        if "__pycache__" in rel.parts or _is_ui_file(rel):
            continue
        try:
            with warnings.catch_warnings():
                # Escape-sequence warnings in scanned files are not this tool's concern.
                warnings.simplefilter("ignore")
                tree = ast.parse(path.read_bytes(), filename=str(path))
        except SyntaxError as exc:
            problems.append(
                f"{rel.as_posix()}:{exc.lineno}: cannot parse ({exc.msg}); "
                "boundary not checked (fail closed)"
            )
            continue
        for line, name in _qt_imports(tree):
            problems.append(
                f"{rel.as_posix()}:{line}: imports {name} outside {UI_DIR.as_posix()}/ (Qt boundary)"
            )
        for line, name, form in _dynamic_qt_imports(tree):
            problems.append(
                f"{rel.as_posix()}:{line}: dynamically imports {name} via {form} "
                f"outside {UI_DIR.as_posix()}/ (Qt boundary)"
            )

    for path in _repo_files(root):
        rel = path.relative_to(root).as_posix()
        lowered = path.read_bytes().lower()
        for ref in FORBIDDEN_REFS:
            if ref.lower().encode("utf-8") in lowered:
                problems.append(f"{rel}: references the meridian-latex engine path {ref!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the Qt and meridian-latex boundary rules.")
    parser.add_argument("--root", type=Path, default=REPO, help="tree to scan (default: this repo)")
    args = parser.parse_args(argv)

    problems = check(args.root)
    for problem in problems:
        print(f"VIOLATION {problem}")
    if problems:
        print(f"boundary FAILED: {len(problems)} violation(s)")
        return 1
    print("boundary ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
