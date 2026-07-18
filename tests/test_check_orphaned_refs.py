"""178ed93e — pre-merge AST check for orphaned local references.

Covers the dedicated AST name-resolution walk that replaces the too-noisy
graph CALLS-confidence approach (691d367b): stale `from X import Y` /
`module.attr` references left behind by a rename or removal must be caught,
while legitimate patterns (star-imports, dynamic `globals().update` re-export
shims, platform-conditional top-level defs, relative imports at every level)
must NOT be flagged.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# scripts/ is not a package — load the module directly by path. Register it in
# sys.modules first: the module's dataclasses (with `from __future__ import
# annotations`) resolve their field types via sys.modules[cls.__module__] at
# class-creation time, which fails if the module isn't registered yet.
_spec = importlib.util.spec_from_file_location(
    "check_orphaned_refs",
    Path(__file__).resolve().parent.parent / "scripts" / "check_orphaned_refs.py",
)
check_orphaned_refs = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = check_orphaned_refs
_spec.loader.exec_module(check_orphaned_refs)  # type: ignore[union-attr]


def _write(root: Path, rel: str, source: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _run(root: Path, targets: list[str]) -> list:
    """Point the module at a synthetic repo root and return findings."""
    files = []
    for raw in targets:
        base = root / raw
        if base.exists():
            files.extend(sorted(base.rglob("*.py")))
    index = check_orphaned_refs.build_module_index(files)
    findings = []
    for path in files:
        findings.extend(check_orphaned_refs.check_file(path, index))
    return findings


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(check_orphaned_refs, "ROOT", tmp_path)
    return tmp_path


def test_renamed_function_leaves_dangling_import(fake_repo):
    _write(fake_repo, "pkg/__init__.py", "")
    _write(fake_repo, "pkg/util.py", "def new_name():\n    return 1\n")
    _write(
        fake_repo,
        "pkg/caller.py",
        "from pkg.util import old_name\n\n\ndef use():\n    return old_name()\n",
    )
    findings = _run(fake_repo, ["pkg"])
    assert len(findings) == 1
    assert findings[0].kind == "import"
    assert "old_name" in findings[0].reference
    assert "pkg.util" in findings[0].reason


def test_renamed_function_leaves_dangling_attribute_access(fake_repo):
    _write(fake_repo, "pkg/__init__.py", "")
    _write(fake_repo, "pkg/util.py", "def new_name():\n    return 1\n")
    _write(
        fake_repo,
        "pkg/caller.py",
        "from pkg import util\n\n\ndef use():\n    return util.old_name()\n",
    )
    findings = _run(fake_repo, ["pkg"])
    assert len(findings) == 1
    assert findings[0].kind == "attribute"
    assert findings[0].reference == "util.old_name"


def test_valid_reference_is_not_flagged(fake_repo):
    _write(fake_repo, "pkg/__init__.py", "")
    _write(fake_repo, "pkg/util.py", "def helper():\n    return 1\n")
    _write(
        fake_repo,
        "pkg/caller.py",
        "from pkg.util import helper\nfrom pkg import util\n\n\n"
        "def use():\n    return helper() + util.helper()\n",
    )
    assert _run(fake_repo, ["pkg"]) == []


def test_relative_imports_resolve_at_every_level(fake_repo):
    _write(fake_repo, "pkg/__init__.py", "def top_helper():\n    return 1\n")
    _write(fake_repo, "pkg/sub/__init__.py", "def sub_helper():\n    return 2\n")
    _write(
        fake_repo,
        "pkg/sub/mod.py",
        "from . import sub_helper\n"
        "from .. import top_helper\n\n\n"
        "def use():\n    return sub_helper() + top_helper()\n",
    )
    assert _run(fake_repo, ["pkg"]) == []


def test_relative_import_dangling_reference_is_caught(fake_repo):
    _write(fake_repo, "pkg/__init__.py", "")
    _write(fake_repo, "pkg/sub/__init__.py", "def sub_helper():\n    return 2\n")
    _write(
        fake_repo,
        "pkg/sub/mod.py",
        "from . import gone_helper\n\n\ndef use():\n    return gone_helper()\n",
    )
    findings = _run(fake_repo, ["pkg"])
    assert len(findings) == 1
    assert "gone_helper" in findings[0].reference


def test_star_import_target_is_not_flagged(fake_repo):
    _write(fake_repo, "pkg/__init__.py", "")
    _write(fake_repo, "pkg/util.py", "from os.path import *\n")
    _write(fake_repo, "pkg/caller.py", "from pkg.util import anything_at_all\n")
    assert _run(fake_repo, ["pkg"]) == []


def test_dynamic_globals_update_shim_is_not_flagged(fake_repo):
    """Compat-shim pattern (d45c2cc8): `globals().update(vars(_impl))`
    re-exports an entire relocated module's namespace dynamically. The
    checker can't enumerate those names via AST, so it must treat the
    module as opaque rather than flag every importer."""
    _write(fake_repo, "pkg/__init__.py", "")
    _write(
        fake_repo,
        "pkg/shim.py",
        "from pkg import util as _impl\n"
        "globals().update({k: v for k, v in vars(_impl).items() if not k.startswith('__')})\n"
        "del _impl\n",
    )
    _write(fake_repo, "pkg/util.py", "def real_func():\n    return 1\n")
    _write(
        fake_repo,
        "pkg/caller.py",
        "from pkg.shim import anything_reexported\n\n\n"
        "def use():\n    return anything_reexported()\n",
    )
    assert _run(fake_repo, ["pkg"]) == []


def test_conditional_top_level_definition_is_not_flagged(fake_repo):
    """A name defined inside a top-level try/except or if/else (platform
    fallback pattern) still counts as a legitimate module export."""
    _write(
        fake_repo,
        "pkg/util.py",
        "try:\n"
        "    def helper():\n"
        "        return 1\n"
        "except ImportError:\n"
        "    def helper():\n"
        "        return 2\n",
    )
    _write(fake_repo, "pkg/__init__.py", "")
    _write(fake_repo, "pkg/caller.py", "from pkg.util import helper\n")
    assert _run(fake_repo, ["pkg"]) == []


def test_third_party_import_is_ignored(fake_repo):
    _write(fake_repo, "pkg/__init__.py", "")
    _write(fake_repo, "pkg/caller.py", "from unknown_third_party import whatever\n")
    assert _run(fake_repo, ["pkg"]) == []


def test_real_repo_is_clean():
    """Guardrail: the actual first-party source tree must have zero orphaned
    local references. If this starts failing, either a real rename left a
    dangling caller (fix the caller) or a new dynamic-export pattern needs
    the same opaque-module treatment as the docs_intel/latex_intel shims."""
    files = check_orphaned_refs.iter_source_files()
    files = [f for f in files if f.name != check_orphaned_refs.SELF_NAME]
    index = check_orphaned_refs.build_module_index(files + [Path(check_orphaned_refs.__file__).resolve()])
    findings = []
    for path in files:
        findings.extend(check_orphaned_refs.check_file(path, index))
    assert findings == [], "\n".join(
        f"{f.path}:{f.line_no} {f.reference} -- {f.reason}" for f in findings
    )


def test_main_returns_zero_on_clean_repo(capsys):
    exit_code = check_orphaned_refs.main()
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "0 orphaned local reference(s) found" in captured.out
