"""Mutation check for tools/check_boundary.py.

Each mutant is a copy of the checker with one guard removed or weakened. The
mutant is written under pytest's temp directory and loaded from there, so the
real checker is never modified. A mutant counts as killed when at least one case
from BOUNDARY_CASES (tests/test_boundary.py) fails on it, either with a wrong
result or with an exception.
"""

import importlib.util
from pathlib import Path

from test_boundary import BOUNDARY_CASES, REPO, check_boundary

CHECKER_SOURCE = REPO / "tools" / "check_boundary.py"

# Each mutant maps to (old, new). Every old text must occur exactly once in the
# checker source, so a mutant cannot quietly become a no-op or hit the wrong line.
# If the checker is edited and an anchor stops matching, this test fails and the
# anchor is updated here.
MUTANTS = {
    "drop_pyside2": ('"PySide2", ', ""),
    "drop_shiboken6": ('"shiboken6", ', ""),
    "walk_only_tree_body": ("for node in ast.walk(tree):", "for node in tree.body:"),
    # Only the file content is lowered. The FORBIDDEN_REFS constants are already
    # lower-case, so dropping ref.lower() would be an equivalent mutant.
    "drop_lower": ("lowered = path.read_bytes().lower()", "lowered = path.read_bytes()"),
    "remove_syntax_error_branch": (
        '''        try:
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
''',
        """        with warnings.catch_warnings():
            # Escape-sequence warnings in scanned files are not this tool's concern.
            warnings.simplefilter("ignore")
            tree = ast.parse(path.read_bytes(), filename=str(path))
""",
    ),
    "build_not_skipped": ('    "build",\n', ""),
    # Dynamic-import rule (a2): each guard in the new helpers has a mutant.
    "dyn_drop_bare_dunder_import": (
        'return "__import__" if func.id == "__import__" else importers.get(func.id)',
        'return "__import__" if func.id == "__importx__" else importers.get(func.id)',
    ),
    "dyn_drop_keyword_arg": (
        "    return next((kw.value for kw in call.keywords if kw.arg == keyword), None)",
        "    return None",
    ),
    "dyn_drop_positional_arg": ("    if index < len(call.args):", "    if False:"),
    "dyn_drop_module_alias": (
        "modules[alias.asname or alias.name] = alias.name",
        "modules[alias.name] = alias.name",
    ),
    "dyn_drop_from_import_alias": (
        "importers[alias.asname or alias.name] = dotted",
        "importers[alias.name] = dotted",
    ),
    "dyn_drop_default_modules": (
        "modules = {name: name for name in IMPORT_MODULES}",
        "modules = {}",
    ),
    "dyn_drop_builtins_module": (
        'IMPORT_MODULES = ("builtins", "importlib")',
        'IMPORT_MODULES = ("importlib",)',
    ),
    "dyn_drop_builtins_importer": ('"builtins.__import__", ', ""),
    "dyn_drop_importlib_dunder_importer": ('"importlib.__import__", ', ""),
    "dyn_drop_import_module_importer": (', "importlib.import_module"}', "}"),
    "dyn_drop_from_import_level_gate": (
        "elif isinstance(item, ast.ImportFrom) and item.level == 0 and item.module in IMPORT_MODULES:",
        "elif isinstance(item, ast.ImportFrom) and item.module in IMPORT_MODULES:",
    ),
    "dyn_drop_attribute_form": (
        "if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):",
        "if False:",
    ),
    "dyn_drop_relative_check": ('if name.startswith("."):', 'if name.startswith("!"):'),
    "dyn_drop_relative_package": (
        '_str_arg(call, 1, "package") if form == "importlib.import_module" else None',
        "None",
    ),
    "dyn_drop_relative_package_root_check": (
        'if package is None or package.split(".")[0] not in QT_ROOTS:',
        "if package is None:",
    ),
    "dyn_drop_level_gate": ('if form.endswith("__import__"):', "if False:"),
    "dyn_drop_level_threshold": ("level.value > 0:", "level.value > 99:"),
    "dyn_drop_root_check": (
        'return name if name.split(".")[0] in QT_ROOTS else None',
        "return name",
    ),
    "dyn_drop_source_runner_check": ("call.func.id in SOURCE_RUNNERS:", "call.func.id in ():"),
    "dyn_drop_source_runner_exec": ('SOURCE_RUNNERS = ("eval", "exec")', 'SOURCE_RUNNERS = ("eval",)'),
    "dyn_drop_source_bytes": ("isinstance(source.value, (str, bytes))", "isinstance(source.value, str)"),
    "dyn_drop_source_static_imports": (
        "found = [(call.lineno, name, where) for _, name in _qt_imports(inner)]",
        "found = []",
    ),
    "dyn_drop_source_nested_imports": (
        "for _, name, form in _dynamic_qt_imports(inner):",
        "for _, name, form in []:",
    ),
    "dyn_drop_source_parse_guard": (
        "    except SyntaxError:\n        return []  # the runner raises before it can import anything\n",
        "    except KeyError:\n        return []  # the runner raises before it can import anything\n",
    ),
    "dyn_walk_only_tree_body": ("for call in ast.walk(tree):", "for call in tree.body:"),
    "dyn_drop_wiring": (
        "        for line, name, form in _dynamic_qt_imports(tree):",
        "        for line, name, form in []:",
    ),
}


def _mutate(source: str, name: str) -> str:
    old, new = MUTANTS[name]
    found = source.count(old)
    assert found == 1, f"mutant {name}: anchor found {found} times, expected exactly 1"
    return source.replace(old, new)


def _load(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _first_failing_case(checker, base: Path):
    """Run every boundary case against checker. Return (case, error) for the first failure, else None."""
    for case_name, case in BOUNDARY_CASES:
        root = base / case_name
        root.mkdir(parents=True)
        try:
            case(checker, root)
        except Exception as exc:  # an AssertionError or an exception from the checker both count
            return case_name, f"{type(exc).__name__}: {exc}"
    return None


def test_every_boundary_mutant_is_killed(tmp_path):
    source = CHECKER_SOURCE.read_text(encoding="utf-8")

    # The real checker must pass every case first. Otherwise every mutant would look killed.
    baseline = _first_failing_case(check_boundary, tmp_path / "baseline")
    assert baseline is None, f"the real checker fails a boundary case: {baseline}"

    survivors = []
    for name in MUTANTS:
        mutant_path = tmp_path / f"mutant_{name}.py"
        mutant_path.write_text(_mutate(source, name), encoding="utf-8")
        mutant = _load(mutant_path, f"check_boundary_mutant_{name}")
        if _first_failing_case(mutant, tmp_path / name) is None:
            survivors.append(name)

    assert survivors == [], f"mutants survived every boundary case: {survivors}"
