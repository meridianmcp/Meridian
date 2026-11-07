"""Measure registry, the no-calibration guarantee, and the pure-Python constraint."""

import ast
import sys
from pathlib import Path

import pytest

from meridian import prose
from meridian.prose import MEASURES, NOT_BUILT, get_measure, measure_table
from meridian.prose.features import lexical_diversity, specificity

PROSE_DIR = Path(prose.__file__).resolve().parent
FORBIDDEN_IMPORTS = {
    "socket", "urllib", "http", "requests", "httpx", "aiohttp", "ssl",
    "ftplib", "smtplib", "xmlrpc", "webbrowser",
}


def test_every_measure_is_uncalibrated_and_not_calibrated():
    assert MEASURES
    for measure in MEASURES:
        assert measure.validation_status == "uncalibrated", measure.name
        assert measure.calibrated is False, measure.name
        assert measure.definition.strip(), measure.name


def test_measure_names_are_unique_and_lookup_works():
    names = [m.name for m in MEASURES]
    assert len(names) == len(set(names))
    assert get_measure("specificity").kind == "value"
    with pytest.raises(KeyError):
        get_measure("no_such_measure")


def test_feature_values_report_the_registry_status():
    for value in (specificity("Meridian uses Postgres."), lexical_diversity("Cache clears daily.")):
        assert value.validation_status == "uncalibrated"
        assert value.calibrated is False


def test_measure_table_is_plain_data():
    table = measure_table()
    assert len(table) == len(MEASURES)
    assert all(isinstance(row, dict) and row["calibrated"] is False for row in table)


def test_not_built_note_records_the_two_deliberate_omissions():
    joined = " ".join(NOT_BUILT).lower()
    assert "calibration protocol" in joined
    assert "house-style" in joined


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_package_uses_only_the_standard_library_and_no_network_modules():
    files = sorted(PROSE_DIR.glob("*.py"))
    assert len(files) >= 9
    for path in files:
        roots = _imported_roots(path)
        non_stdlib = {r for r in roots if r not in sys.stdlib_module_names and r != "__future__"}
        assert not non_stdlib, f"{path.name} imports non-stdlib: {non_stdlib}"
        assert not (roots & FORBIDDEN_IMPORTS), f"{path.name} imports a network module"
