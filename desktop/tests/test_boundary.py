"""Tests for tools/check_boundary.py: a violation tree fails, a clean tree passes,
and the real repo is checked. The real-repo test passes on this repository and
fails only if a file here breaks the Qt or meridian-latex boundary rules."""

import importlib.util
from functools import partial
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("check_boundary", REPO / "tools" / "check_boundary.py")
check_boundary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_boundary)

LATEX_REF = check_boundary.FORBIDDEN_REFS[0]
LATEX_REF_ESCAPED = check_boundary.FORBIDDEN_REFS[2]  # doubled backslash, as in a Python string


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _clean_tree(root: Path) -> None:
    _write(root, "meridian_desktop/__init__.py", '__version__ = "0.1.0"\n')
    _write(root, "meridian_desktop/settings_store.py", "import json\n")
    _write(root, "meridian_desktop/ui/__init__.py", "")
    _write(root, "meridian_desktop/ui/main_window.py", "from PySide6.QtWidgets import QLabel\n")
    _write(root, "meridian_desktop/tray.py", "from PySide6.QtGui import QIcon\n")
    _write(root, "README.md", "# clean\n")


def test_clean_tree_passes(tmp_path):
    _clean_tree(tmp_path)
    assert check_boundary.check(tmp_path) == []


def test_qt_import_outside_ui_fails(tmp_path):
    _clean_tree(tmp_path)
    _write(tmp_path, "meridian_desktop/logic.py", "import PySide6.QtCore\n")
    problems = check_boundary.check(tmp_path)
    assert len(problems) == 1
    assert "logic.py" in problems[0]
    assert "PySide6.QtCore" in problems[0]


def test_from_qt_import_outside_ui_fails(tmp_path):
    _clean_tree(tmp_path)
    _write(tmp_path, "meridian_desktop/logic.py", "from PySide6 import QtWidgets\n")
    problems = check_boundary.check(tmp_path)
    assert any("logic.py" in p and "PySide6" in p for p in problems)


def test_nested_pyqt_import_outside_ui_fails(tmp_path):
    _clean_tree(tmp_path)
    _write(tmp_path, "meridian_desktop/services/status.py", "from PyQt5.QtCore import QTimer\n")
    problems = check_boundary.check(tmp_path)
    assert any("status.py" in p for p in problems)


def test_forbidden_reference_in_doc_fails(tmp_path):
    _clean_tree(tmp_path)
    _write(tmp_path, "docs/notes.md", f"see {LATEX_REF}\n")
    problems = check_boundary.check(tmp_path)
    assert any("notes.md" in p for p in problems)


def test_forbidden_reference_in_python_string_fails(tmp_path):
    _clean_tree(tmp_path)
    # Valid Python: the file holds the escaped form, the value has one backslash.
    _write(tmp_path, "meridian_desktop/paths.py", f'ENGINE = "{LATEX_REF_ESCAPED}"\n')
    problems = check_boundary.check(tmp_path)
    assert any("paths.py" in p for p in problems)


def test_forbidden_single_backslash_reference_fails(tmp_path):
    _clean_tree(tmp_path)
    _write(tmp_path, "docs/paths.md", f"path: {check_boundary.FORBIDDEN_REFS[1]}\n")
    problems = check_boundary.check(tmp_path)
    assert any("paths.md" in p for p in problems)


def test_missing_package_fails_closed(tmp_path):
    problems = check_boundary.check(tmp_path)
    assert problems
    assert "fail closed" in problems[0]


def test_violation_tree_exits_1_and_lists_each_violation(tmp_path, capsys):
    _clean_tree(tmp_path)
    _write(tmp_path, "meridian_desktop/a.py", "import PySide6\n")
    _write(tmp_path, "meridian_desktop/b.py", "from PySide6.QtCore import QObject\n")
    code = check_boundary.main(["--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "a.py" in out
    assert "b.py" in out
    assert "boundary ok" not in out


def test_clean_tree_exits_0_with_ok_line(tmp_path, capsys):
    _clean_tree(tmp_path)
    code = check_boundary.main(["--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "boundary ok" in out


def test_pixi_env_is_not_scanned(tmp_path):
    _clean_tree(tmp_path)
    _write(tmp_path, ".pixi/envs/default/share/notes.md", f"see {LATEX_REF}\n")
    assert check_boundary.check(tmp_path) == []


def test_pixi_skip_does_not_hide_same_reference_elsewhere(tmp_path):
    # Control: the skip is limited to the .pixi directory.
    _clean_tree(tmp_path)
    _write(tmp_path, ".pixi/envs/default/share/notes.md", f"see {LATEX_REF}\n")
    _write(tmp_path, "envs/default/share/notes.md", f"see {LATEX_REF}\n")
    problems = check_boundary.check(tmp_path)
    assert len(problems) == 1
    assert "envs/default/share/notes.md" in problems[0]


def test_repo_files_prunes_pixi(tmp_path):
    _write(tmp_path, ".pixi/envs/default/x.txt", "x\n")
    _write(tmp_path, "README.md", "x\n")
    rels = [p.relative_to(tmp_path).as_posix() for p in check_boundary._repo_files(tmp_path)]
    assert rels == ["README.md"]


def test_real_repo_passes():
    problems = check_boundary.check(REPO)
    assert problems == [], "\n".join(problems)


# Qt roots the rule must cover. Written out here rather than read from
# check_boundary.QT_ROOTS, so that dropping a root from the checker is caught.
QT_ROOT_NAMES = ("PySide6", "PySide2", "PyQt5", "PyQt6", "PyQt4", "shiboken6", "shiboken2")


# Each case_* function takes a checker module and an empty directory, builds a
# tree, runs check(), and raises AssertionError on a wrong result. The tests below
# run them against the real checker. tests/test_boundary_mutation.py runs the same
# cases (via BOUNDARY_CASES) against mutated copies of the checker.


def case_qt_root_outside_ui_fails(checker, root, *, qt_root):
    _clean_tree(root)
    _write(root, "meridian_desktop/logic.py", f"from {qt_root}.QtCore import QObject\n")
    problems = checker.check(root)
    assert len(problems) == 1, problems
    assert "logic.py" in problems[0]
    assert qt_root in problems[0]


def case_import_inside_function_body_fails(checker, root):
    _clean_tree(root)
    _write(
        root,
        "meridian_desktop/logic.py",
        "def load_qt():\n    from PySide6.QtCore import QObject\n    return QObject\n",
    )
    problems = checker.check(root)
    assert len(problems) == 1, problems
    assert "logic.py" in problems[0]
    assert "PySide6" in problems[0]


def case_uppercase_windows_reference_fails(checker, root):
    _clean_tree(root)
    # The Windows form of the engine path, upper-cased. The scan ignores case.
    upper_ref = checker.FORBIDDEN_REFS[1].upper()
    _write(root, "docs/notes.md", f"path: {upper_ref}\n")
    problems = checker.check(root)
    assert len(problems) == 1, problems
    assert "notes.md" in problems[0]


def case_syntax_error_fails_closed(checker, root):
    _clean_tree(root)
    _write(root, "meridian_desktop/broken.py", "def broken(:\n    pass\n")
    problems = checker.check(root)
    assert len(problems) == 1, problems
    assert "broken.py" in problems[0]
    assert "fail closed" in problems[0]


def case_build_tree_is_not_scanned(checker, root):
    _clean_tree(root)
    _write(root, "build/lib/notes.md", f"see {LATEX_REF}\n")
    assert checker.check(root) == []


# Dynamic imports (rule a2). Each snippet loads a Qt root by a literal name outside ui/
# and must produce exactly one violation, on the given line, naming the expected text.
DYNAMIC_QT_CALLS = {
    "importlib.import_module": {
        "source": "import importlib\n\nQT = importlib.import_module('PySide6.QtCore')\n",
        "line": 3,
        "qt_name": "PySide6.QtCore",
    },
    "__import__": {
        "source": "QT = __import__('PySide6.QtCore')\n",
        "line": 1,
        "qt_name": "PySide6.QtCore",
    },
}
DYNAMIC_QT_FORMS = {
    "module_alias": {
        "source": "import importlib as il\n\nQT = il.import_module('PySide6.QtGui')\n",
        "line": 3,
        "qt_name": "PySide6.QtGui",
    },
    "from_import_alias": {
        "source": "from importlib import import_module as load\n\nQT = load('PySide6.QtGui')\n",
        "line": 3,
        "qt_name": "PySide6.QtGui",
    },
    "importlib_dunder_import": {
        "source": "import importlib\n\nQT = importlib.__import__('PySide6')\n",
        "line": 3,
        "qt_name": "PySide6",
    },
    "builtins_dunder_import": {
        "source": "import builtins\n\nQT = builtins.__import__('PySide6')\n",
        "line": 3,
        "qt_name": "PySide6",
    },
    "from_builtins_import": {
        "source": "from builtins import __import__ as load\n\nQT = load('PySide6')\n",
        "line": 3,
        "qt_name": "PySide6",
    },
    "keyword_name": {
        "source": "import importlib\n\nQT = importlib.import_module(name='PySide6.QtCore')\n",
        "line": 3,
        "qt_name": "PySide6.QtCore",
    },
    "keyword_name_dunder": {
        "source": "QT = __import__(name='PySide6')\n",
        "line": 1,
        "qt_name": "PySide6",
    },
    "relative_with_package": {
        "source": "import importlib\n\nQT = importlib.import_module('.QtCore', 'PySide6')\n",
        "line": 3,
        "qt_name": ".QtCore in package PySide6",
    },
    "function_body": {
        "source": (
            "import importlib\n\n\ndef load():\n    return importlib.import_module('PySide6.QtCore')\n"
        ),
        "line": 5,
        "qt_name": "PySide6.QtCore",
    },
    "exec_literal": {
        "source": "exec('import PySide6')\n",
        "line": 1,
        "qt_name": "PySide6",
    },
    "eval_literal_dunder": {
        "source": "VALUE = eval(\"__import__('PySide6.QtCore')\")\n",
        "line": 1,
        "qt_name": "PySide6.QtCore",
    },
    "exec_bytes_literal": {
        "source": "exec(b'import PySide6')\n",
        "line": 1,
        "qt_name": "PySide6",
    },
    "exec_uses_module_importlib": {
        "source": "import importlib\n\nexec(\"importlib.import_module('PySide6')\")\n",
        "line": 3,
        "qt_name": "PySide6",
    },
}

# Dynamic imports that must not be flagged: non-Qt names, near-miss roots, and
# relative names whose package is not Qt.
DYNAMIC_NON_QT_SOURCE = (
    "import builtins\n"
    "import importlib\n"
    "\n"
    "JSON = importlib.import_module('json')\n"
    "OS_PATH = __import__('os.path')\n"
    "COLLECTIONS = builtins.__import__('collections')\n"
    "NEAR_MISS = importlib.import_module('PySide6_helpers')\n"
    "RELATIVE = importlib.import_module('.helpers', 'meridian_desktop')\n"
    "exec('import json')\n"
)


def case_dynamic_qt_import_outside_ui_fails(checker, root, *, source, line, qt_name):
    _clean_tree(root)
    _write(root, "meridian_desktop/logic.py", source)
    problems = checker.check(root)
    assert len(problems) == 1, problems
    assert f"logic.py:{line}:" in problems[0], problems[0]
    assert qt_name in problems[0], problems[0]


def case_dynamic_non_qt_import_passes(checker, root):
    _clean_tree(root)
    _write(root, "meridian_desktop/logic.py", DYNAMIC_NON_QT_SOURCE)
    assert checker.check(root) == []


def case_dynamic_qt_import_in_ui_passes(checker, root):
    _clean_tree(root)
    _write(
        root,
        "meridian_desktop/ui/dynamic.py",
        "import importlib\nQT = importlib.import_module('PySide6.QtCore')\n",
    )
    _write(root, "meridian_desktop/tray.py", "QT = __import__('PySide6.QtGui')\n")
    assert checker.check(root) == []


def case_dynamic_relative_import_passes(checker, root):
    # A relative from-import is a package's own module, not the stdlib one, and a
    # relative __import__ resolves against the caller's package.
    _clean_tree(root)
    _write(
        root,
        "meridian_desktop/logic.py",
        "from .importlib import import_module\n"
        "\n"
        "PLUGIN = import_module('PySide6')\n"
        "LEVEL = __import__('PySide6', None, None, None, 1)\n",
    )
    assert checker.check(root) == []


def case_unparseable_source_literal_passes(checker, root):
    # The runner raises SyntaxError before any import runs, so there is nothing to flag.
    _clean_tree(root)
    _write(root, "meridian_desktop/logic.py", "exec('def(')\n")
    assert checker.check(root) == []


BOUNDARY_CASES = [
    (f"qt_root_{name}", partial(case_qt_root_outside_ui_fails, qt_root=name))
    for name in QT_ROOT_NAMES
] + [
    ("import_inside_function_body", case_import_inside_function_body_fails),
    ("uppercase_windows_reference", case_uppercase_windows_reference_fails),
    ("syntax_error_fails_closed", case_syntax_error_fails_closed),
    ("build_tree_not_scanned", case_build_tree_is_not_scanned),
] + [
    (f"dynamic_{key.replace('.', '_')}", partial(case_dynamic_qt_import_outside_ui_fails, **spec))
    for key, spec in {**DYNAMIC_QT_CALLS, **DYNAMIC_QT_FORMS}.items()
] + [
    ("dynamic_non_qt_passes", case_dynamic_non_qt_import_passes),
    ("dynamic_in_ui_passes", case_dynamic_qt_import_in_ui_passes),
    ("dynamic_relative_passes", case_dynamic_relative_import_passes),
    ("unparseable_source_literal_passes", case_unparseable_source_literal_passes),
]


@pytest.mark.parametrize("qt_root", QT_ROOT_NAMES)
def test_each_other_qt_root_outside_ui_fails(tmp_path, qt_root):
    case_qt_root_outside_ui_fails(check_boundary, tmp_path, qt_root=qt_root)


def test_import_inside_function_body_fails(tmp_path):
    case_import_inside_function_body_fails(check_boundary, tmp_path)


def test_uppercase_windows_reference_fails(tmp_path):
    case_uppercase_windows_reference_fails(check_boundary, tmp_path)


def test_syntax_error_fails_closed(tmp_path):
    case_syntax_error_fails_closed(check_boundary, tmp_path)


def test_build_tree_is_not_scanned(tmp_path):
    case_build_tree_is_not_scanned(check_boundary, tmp_path)


@pytest.mark.parametrize("form", sorted(DYNAMIC_QT_CALLS))
def test_dynamic_qt_import_outside_ui_fails(tmp_path, form):
    case_dynamic_qt_import_outside_ui_fails(check_boundary, tmp_path, **DYNAMIC_QT_CALLS[form])


@pytest.mark.parametrize("form", sorted(DYNAMIC_QT_FORMS))
def test_dynamic_import_form_outside_ui_fails(tmp_path, form):
    case_dynamic_qt_import_outside_ui_fails(check_boundary, tmp_path, **DYNAMIC_QT_FORMS[form])


def test_dynamic_non_qt_import_passes(tmp_path):
    case_dynamic_non_qt_import_passes(check_boundary, tmp_path)


def test_dynamic_qt_import_in_ui_passes(tmp_path):
    case_dynamic_qt_import_in_ui_passes(check_boundary, tmp_path)


def test_dynamic_relative_import_passes(tmp_path):
    case_dynamic_relative_import_passes(check_boundary, tmp_path)


def test_unparseable_source_literal_passes(tmp_path):
    case_unparseable_source_literal_passes(check_boundary, tmp_path)
