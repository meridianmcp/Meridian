"""Tests for 4f3765c1 -- isolate subprocess-sensitive tests from xdist and
preserve truthful failure classification.

Windows subprocess-teardown crashes (NTSTATUS codes such as ACCESS_VIOLATION,
DLL_NOT_FOUND, and CONTROL_C_EXIT -- see test_code_intel_guard.py's and
test_14575683_jq_fastpath_ci_guard.py's own ``_WIN_CRASH_CODES`` retry
workaround) have been observed "under heavy xdist (-n auto) contention" when
many pytest-xdist workers each spawn a real bash/PowerShell/python child
process concurrently. Retrying inside each test masks the SYMPTOM per-test
but does nothing about the CONTENTION that causes it.

This item isolates every test that spawns a real OS subprocess behind a new
``subprocess_isolated`` pytest marker (registered in pytest.ini), excludes
those tests from the main ``-n auto`` sweep, and reruns them as their own
dedicated SERIAL step in pixi.toml's ``test``/``test-pg``/``test-cov``
tasks -- the exact isolation pattern already proven for
``tests/test_rate_limit_serial.py`` (a different flake mechanism: wall-clock
sensitivity, not subprocess contention), applied here. Routing the new step
through ``scripts/run_tests.py`` (never a raw ``pytest`` invocation) is what
PRESERVES truthful failure classification: the durable ``TestRunRecord``
state machine (2cebf4ae) -- STALLED/TIMED_OUT/CRASHED vs PASSED/FAILED
classification, the Windows PID-liveness fix, real exit-code propagation --
already applies to the other two pixi test steps; a raw ``pytest`` call for
the new step would silently lose all of that for exactly the tests most
prone to infra-level crashes.

These tests are themselves plain, in-process, non-subprocess checks (static
source/config inspection) -- they must never need the ``subprocess_isolated``
marker themselves, and must never be flaky for the same reason they exist to
guard against.
"""
from __future__ import annotations

import ast
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_PYTEST_INI = _REPO / "pytest.ini"
_PIXI_TOML = _REPO / "pixi.toml"

_MARKER = "subprocess_isolated"

# Every top-level test function across the three touched files that spawns a
# real OS subprocess (directly, or via a helper it calls) and therefore MUST
# carry @pytest.mark.subprocess_isolated.
_ISOLATED_TESTS: dict[str, set[str]] = {
    "tests/test_86b36617_tool_discovery.py": {
        "test_run_targeted_tests_propagates_real_nonzero_exit_code",
        "test_run_targeted_tests_propagates_real_zero_exit_code",
        "test_run_targeted_tests_parses_pytest_style_pass_fail_counts",
        "test_run_targeted_tests_timeout_kills_process_and_reports_status",
        "test_run_targeted_tests_shell_pipe_can_mask_exit_code_list_form_cannot",
    },
    "tests/test_code_intel_guard.py": {
        "test_hook_fails_open_when_server_unreachable",
        "test_hook_fails_open_when_code_intel_disabled",
        "test_hook_blocks_grep_glob_when_code_intel_enabled",
        "test_hook_stderr_names_the_tool_that_was_blocked",
        "test_hook_allows_all_other_tools",
        "test_hook_fails_open_on_unparseable",
        "test_hook_fails_open_when_slot_readiness_unreachable_but_settings_ok",
        "test_hook_fails_open_when_slot_readiness_body_unparseable",
        "test_hook_fails_open_when_slot_readiness_json_missing_fields",
        "test_ps1_hook_fails_open_when_code_intel_disabled",
        "test_ps1_hook_fails_open_when_server_unreachable",
        "test_ps1_hook_fails_open_when_slot_readiness_unreachable",
        "test_ps1_hook_fails_open_when_slot_readiness_malformed",
        "test_ps1_hook_fails_open_when_slot_readiness_missing_fields",
        "test_ps1_hook_fails_open_when_ready_false",
        "test_ps1_hook_fails_open_when_has_tunnel_false",
        "test_ps1_hook_blocks_when_validated_ready_and_tunnel",
        "test_ps1_hook_fails_open_on_non_boolean_ready_or_tunnel",
        "test_ps1_hook_fails_open_on_unparseable_payload",
        "test_ps1_hook_allows_all_other_tools",
        "test_ps1_hook_is_pure_ascii_and_parses_with_zero_errors",
    },
    "tests/test_14575683_jq_fastpath_ci_guard.py": {
        "test_jq_tool_name_structural_extraction_ignores_nested_decoy",
        "test_jq_ready_false_triggers_failopen_not_ready_warning",
        "test_jq_has_tunnel_false_triggers_failopen_no_tunnel_warning",
        "test_jq_slot_readiness_ignores_nested_decoy_ready_field",
        "test_jq_slot_readiness_missing_fields_fails_open_not_block",
    },
}

def _decorator_names(node) -> set[str]:
    """Dotted decorator names for one function def, e.g. {"_needs_bash",
    "pytest.mark.subprocess_isolated"} -- covers bare-name decorators
    (``@_needs_bash``), attribute-chain marks (``@pytest.mark.parametrize``),
    and calls of either form."""
    names: set[str] = set()
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        parts: list[str] = []
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if isinstance(target, ast.Name):
            parts.append(target.id)
        if parts:
            names.add(".".join(reversed(parts)))
    return names


_FUNC_NODE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _collect_test_markers(rel_path: str) -> dict[str, set[str]]:
    """Map each top-level ``test_*`` function (sync OR async -- several of
    the isolated tests are ``async def``) in *rel_path* to its own decorator
    name set. Deliberately top-level only (module-level functions) -- every
    test named in ``_ISOLATED_TESTS`` lives at module scope in all three
    target files; DB-fixture tests nested in classes are out of scope for
    this item (none of them spawn a subprocess)."""
    source = (_REPO / rel_path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=rel_path)
    result: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, _FUNC_NODE_TYPES) and node.name.startswith("test_"):
            result[node.name] = _decorator_names(node)
    return result


def _marked_names(markers: dict[str, set[str]]) -> set[str]:
    return {
        name
        for name, decs in markers.items()
        if any(d == _MARKER or d.endswith(f".{_MARKER}") for d in decs)
    }


class TestMarkerRegistered:
    def test_pytest_ini_registers_subprocess_isolated_marker(self):
        text = _PYTEST_INI.read_text(encoding="utf-8")
        assert "markers" in text
        assert _MARKER in text, (
            f"pytest.ini must register the '{_MARKER}' marker -- avoids "
            "PytestUnknownMarkWarning and documents the isolation contract"
        )


class TestIsolatedTestsAreMarked:
    def test_declared_subprocess_tests_exist_in_source(self):
        """Sanity check on the fixture data itself: every name in
        _ISOLATED_TESTS must actually be a real top-level test in that
        file (catches a typo'd/renamed test name in this data, not just in
        the source)."""
        missing = []
        for rel_path, names in _ISOLATED_TESTS.items():
            markers = _collect_test_markers(rel_path)
            missing.extend(f"{rel_path}::{n}" for n in names if n not in markers)
        assert not missing, f"declared but not found in source: {missing}"

    def test_marked_tests_exactly_match_the_declared_isolated_set(self):
        """The set of tests actually carrying @pytest.mark.subprocess_isolated
        in each file must be EXACTLY the declared _ISOLATED_TESTS set -- not
        a subset (under-marking leaves a subprocess test back in the flaky
        -n auto sweep) and not a superset (over-marking needlessly shrinks
        the parallel sweep for a test that never spawns a process)."""
        mismatches = {}
        for rel_path, expected in _ISOLATED_TESTS.items():
            actual = _marked_names(_collect_test_markers(rel_path))
            if actual != expected:
                mismatches[rel_path] = {
                    "missing_marker": sorted(expected - actual),
                    "unexpectedly_marked": sorted(actual - expected),
                }
        assert not mismatches, mismatches


class TestPixiTomlWiring:
    """The pixi.toml test/test-pg/test-cov tasks must exclude
    subprocess_isolated tests from the main sweep and rerun them as their
    own `scripts/run_tests.py --serial` step (never raw pytest, so the
    truthful TestRunRecord classification in scripts/run_tests.py still
    covers this step)."""

    def _task(self, name: str) -> str:
        import tomllib

        with _PIXI_TOML.open("rb") as fh:
            data = tomllib.load(fh)
        return data["tasks"][name]

    def test_pixi_toml_parses(self):
        import tomllib

        with _PIXI_TOML.open("rb") as fh:
            tomllib.load(fh)  # must not raise

    def _assert_wired(self, task: str) -> None:
        # Main sweep excludes the isolated tests.
        assert '-m "not subprocess_isolated"' in task, task
        # A dedicated serial rerun exists, selecting exactly the isolated set.
        assert "--serial" in task
        assert "-m subprocess_isolated" in task
        # The isolated rerun is routed through the truthful-classification
        # wrapper, never a bare `pytest` invocation.
        assert "scripts/run_tests.py --serial tests/ " in task or (
            "scripts/run_tests.py --serial tests/" in task
        )

    def test_test_task_isolates_subprocess_tests(self):
        self._assert_wired(self._task("test"))

    def test_test_pg_task_isolates_subprocess_tests(self):
        self._assert_wired(self._task("test-pg"))

    def test_test_cov_task_isolates_subprocess_tests(self):
        task = self._task("test-cov")
        self._assert_wired(task)
        # Coverage must accumulate across all three steps and only the FINAL
        # step reports/enforces the threshold (matches the pre-existing
        # test_rate_limit_serial.py accumulation pattern).
        assert task.count("--cov-append") == 2
        assert "--cov-fail-under=80" in task
        assert task.rindex("--cov-fail-under=80") > task.rindex("subprocess_isolated")

    def test_rate_limit_serial_isolation_untouched(self):
        """This item must not regress the pre-existing, unrelated
        test_rate_limit_serial.py isolation step."""
        for name in ("test", "test-pg", "test-cov"):
            task = self._task(name)
            assert "--serial tests/test_rate_limit_serial.py" in task, name
