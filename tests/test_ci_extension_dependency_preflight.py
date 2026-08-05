"""Unit coverage for scripts/preflight_extension_dependencies.py.

Sprint item 0ab8139f-c482-450b-825d-852711d12f30: this preflight is the CI
gate that turns "pip silently resolved an incompatible mcp 2.x and pytest
collection died with a ModuleNotFoundError" (GitHub Actions run 31047597879)
into an early, named, actionable failure. These tests exercise the script's
pure functions directly rather than shelling out, per that item's "the
preflight is covered by tests" acceptance criterion -- a workflow YAML step
itself can't practically be unit-tested, but the Python script backing it
can.

Companion coverage: tests/test_tunnel_extension_dependencies.py asserts the
*current* manifests are pinned correctly and that both entry points import
under whatever mcp major is installed in the main test environment. This
file instead tests the preflight *script itself*: does it correctly accept a
well-formed pin, correctly reject a stale/unbounded one with an actionable
message, and correctly report the SHA + manifest values it checked.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import preflight_extension_dependencies as preflight  # noqa: E402


# ---------------------------------------------------------------------------
# parse_requirement / find_dependency
# ---------------------------------------------------------------------------


def test_parse_requirement_extracts_name_and_clauses() -> None:
    req = preflight.parse_requirement("mcp>=1.27,<2")
    assert req.name == "mcp"
    assert req.clauses == [(">=", "1.27"), ("<", "2")]


def test_parse_requirement_strips_extras() -> None:
    req = preflight.parse_requirement("mcp[extra]>=1.27,<2")
    assert req.name == "mcp"


def test_parse_requirement_with_no_specifier_has_no_clauses() -> None:
    req = preflight.parse_requirement("mcp")
    assert req.name == "mcp"
    assert req.clauses == []


def test_find_dependency_missing_raises() -> None:
    with pytest.raises(preflight.PreflightError, match="no 'mcp' dependency"):
        preflight.find_dependency(["latex2mathml>=2.0", "duckdb>=0.10"])


# ---------------------------------------------------------------------------
# validate_mcp_specifier -- well-formed pins pass, INCLUDING a bumped range
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dep_string",
    [
        "mcp>=1.27,<2",
        "mcp>=1.28,<2",  # a legitimate future bump must still pass -- not a literal string compare
        "mcp>=1.99,<2",
        "mcp>=1.27,<=1.99",
    ],
)
def test_well_formed_pin_passes(dep_string: str) -> None:
    req = preflight.parse_requirement(dep_string)
    preflight.validate_mcp_specifier(req)  # must not raise


# ---------------------------------------------------------------------------
# validate_mcp_specifier -- deliberately stale/unbounded manifests FAIL with
# an actionable message (the item's core acceptance criterion)
# ---------------------------------------------------------------------------


def test_stale_manifest_no_upper_bound_fails_with_actionable_message() -> None:
    # A valid lower bound but no upper bound at all -- isolates the "no
    # upper bound" failure path specifically.
    req = preflight.parse_requirement("mcp>=1.27")
    with pytest.raises(preflight.PreflightError) as exc_info:
        preflight.validate_mcp_specifier(req)
    message = str(exc_info.value)
    assert "no upper bound" in message
    assert "mcp==2.0.0" in message or "2.0" in message
    assert "31047597879" in message  # actionable: points at the incident


def test_historical_bug_manifest_state_fails() -> None:
    # This is literally the committed manifest state that broke CI run
    # 31047597879 before commit 324bfbf: mcp>=1.0 with no upper bound at
    # all. It violates BOTH rules (lower bound too low, no upper bound);
    # either reason is a correct fail-closed result, so just assert it is
    # rejected with an actionable, non-empty message.
    req = preflight.parse_requirement("mcp>=1.0")
    with pytest.raises(preflight.PreflightError) as exc_info:
        preflight.validate_mcp_specifier(req)
    assert str(exc_info.value)


def test_stale_manifest_low_lower_bound_fails_with_actionable_message() -> None:
    req = preflight.parse_requirement("mcp>=1.0,<2")
    with pytest.raises(preflight.PreflightError) as exc_info:
        preflight.validate_mcp_specifier(req)
    message = str(exc_info.value)
    assert "1.27" in message
    assert "below the required minimum" in message


def test_upper_bound_that_still_permits_v2_fails() -> None:
    # Has *an* upper bound clause, but not one that actually excludes 2.x --
    # proves the check evaluates the specifier rather than just checking
    # "is there a '<' somewhere".
    req = preflight.parse_requirement("mcp>=1.27,<3")
    with pytest.raises(preflight.PreflightError, match="does not exclude the mcp 2.x major"):
        preflight.validate_mcp_specifier(req)


def test_no_lower_bound_at_all_fails() -> None:
    req = preflight.parse_requirement("mcp<2")
    with pytest.raises(preflight.PreflightError, match="no lower bound"):
        preflight.validate_mcp_specifier(req)


# ---------------------------------------------------------------------------
# check_manifest / check_all_manifests against a fixture file (deliberately
# stale) and against the real, currently-committed repo manifests
# ---------------------------------------------------------------------------


def test_check_manifest_against_stale_fixture_fails(tmp_path, monkeypatch) -> None:
    # Deliberately stale/unbounded manifest fixture (a valid-looking lower
    # bound but no upper bound at all -- pip is free to resolve mcp 2.x).
    fixture = tmp_path / "pyproject.toml"
    fixture.write_text(
        '[project]\n'
        'name = "fake-ext"\n'
        'dependencies = ["mcp>=1.27", "latex2mathml>=2.0"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(preflight, "REPO_ROOT", tmp_path)
    check = preflight.check_manifest("pyproject.toml")
    assert check.ok is False
    assert "no upper bound" in check.message


def test_check_manifest_against_well_formed_fixture_passes(tmp_path, monkeypatch) -> None:
    fixture = tmp_path / "pyproject.toml"
    fixture.write_text(
        '[project]\n'
        'name = "fake-ext"\n'
        'dependencies = ["mcp>=1.27,<2", "latex2mathml>=2.0"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(preflight, "REPO_ROOT", tmp_path)
    check = preflight.check_manifest("pyproject.toml")
    assert check.ok is True
    assert check.dependency_string == "mcp>=1.27,<2"


def test_check_manifest_missing_file_fails() -> None:
    check = preflight.check_manifest("extensions/does-not-exist/pyproject.toml")
    assert check.ok is False
    assert "not found" in check.message


def test_check_all_manifests_against_real_repo_passes() -> None:
    # Regression guard: locks in the currently-committed pin (mcp>=1.27,<2,
    # commit 324bfbf / board item 106caa76) via the preflight script itself,
    # not just tests/test_tunnel_extension_dependencies.py's literal check.
    checks = preflight.check_all_manifests()
    assert len(checks) == 2
    for check in checks:
        assert check.ok, f"{check.manifest_path}: {check.message}"
        assert check.dependency_string == "mcp>=1.27,<2"


# ---------------------------------------------------------------------------
# run_manifest_mode -- CLI-level reporting (SHA + manifest values), exit code
# ---------------------------------------------------------------------------


def test_run_manifest_mode_passes_and_reports_sha_and_deps(capsys) -> None:
    exit_code = preflight.run_manifest_mode()
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "[preflight] checked-out SHA:" in out
    assert "extensions/meridian-docs/pyproject.toml: mcp>=1.27,<2" in out
    assert "extensions/meridian-outputs/pyproject.toml: mcp>=1.27,<2" in out
    assert "PASSED" in out


def test_run_manifest_mode_fails_closed_on_stale_manifest(tmp_path, monkeypatch, capsys) -> None:
    stale = tmp_path / "stale.toml"
    stale.write_text(
        '[project]\n'
        'name = "fake-ext"\n'
        'dependencies = ["mcp>=1.0"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(preflight, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        preflight,
        "EXTENSIONS",
        (("stale.toml", "fake-ext", "fake_ext.server"),),
    )
    exit_code = preflight.run_manifest_mode()
    captured = capsys.readouterr()
    assert exit_code == 1  # non-zero exit
    assert "FAIL stale.toml" in captured.out
    assert "reason:" in captured.out  # actionable message, not a bare traceback
    assert "FAILED" in captured.err


def test_main_manifest_mode_returns_int(monkeypatch) -> None:
    # Confirms the argparse CLI wiring itself (not just the underlying
    # function) round-trips to an int exit code, mirroring how
    # `if __name__ == "__main__": raise SystemExit(main())` uses it.
    exit_code = preflight.main(["manifest"])
    assert exit_code == 0


# ---------------------------------------------------------------------------
# Import checks -- mcp.server.fastmcp and each extension's entry point
# ---------------------------------------------------------------------------


def test_check_fastmcp_importable_in_this_environment() -> None:
    # meridian's own pixi environment already depends on mcp (pixi.toml), so
    # this genuinely exercises the real import in the test environment
    # rather than a mock.
    result = preflight.check_fastmcp_importable()
    assert result.ok, result.message


def test_check_extension_entry_point_missing_module_fails_with_actionable_message() -> None:
    result = preflight.check_extension_entry_point("meridian-docs", "no_such_module_xyz")
    assert result.ok is False
    assert "pip install -e extensions/meridian-docs" in result.message


def test_check_extension_entry_point_success(monkeypatch) -> None:
    from mcp.server.fastmcp import FastMCP

    fake_module = type(sys)("fake_meridian_docs_server")
    fake_module.mcp = FastMCP("fake-meridian-docs")

    real_import_module = preflight.importlib.import_module

    def _fake_import(name: str, *args, **kwargs):
        # Only intercept our fake target; forward everything else (including
        # nested lazy submodule imports triggered along the way, e.g.
        # pydantic's lazy-attribute machinery) to the real import_module so
        # this doesn't break unrelated imports elsewhere in the process.
        if name == "fake_meridian_docs.server":
            return fake_module
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(preflight.importlib, "import_module", _fake_import)
    result = preflight.check_extension_entry_point("meridian-docs", "fake_meridian_docs.server")
    assert result.ok is True


def test_check_extension_entry_point_wrong_type_fails(monkeypatch) -> None:
    fake_module = type(sys)("fake_bad_server")
    fake_module.mcp = object()  # not a FastMCP instance

    real_import_module = preflight.importlib.import_module

    def _fake_import(name: str, *args, **kwargs):
        if name == "fake_bad_server":
            return fake_module
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(preflight.importlib, "import_module", _fake_import)
    result = preflight.check_extension_entry_point("meridian-docs", "fake_bad_server")
    assert result.ok is False
    assert "not a mcp.server.fastmcp.FastMCP instance" in result.message


def test_run_import_mode_reports_sha(capsys) -> None:
    preflight.run_import_mode()
    out = capsys.readouterr().out
    assert "[preflight] checked-out SHA:" in out


# ---------------------------------------------------------------------------
# resolve_commit_sha
# ---------------------------------------------------------------------------


def test_resolve_commit_sha_prefers_github_sha_env(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_SHA", "deadbeef1234")
    assert preflight.resolve_commit_sha() == "deadbeef1234"


def test_resolve_commit_sha_falls_back_to_git(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.delenv("MERIDIAN_GIT_SHA", raising=False)
    sha = preflight.resolve_commit_sha()
    assert sha != "unknown"
    assert len(sha) >= 7
