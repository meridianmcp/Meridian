"""Tests for abac2298 — repository-aware executor test gates and honest
recovery from pytest-xdist worker crashes.

Urgent follow-up from workspace proposal 0b7102d4-5ef8-4f17-84b0-157f1bef6268
(Wave 16 friction report, 2026-08-05): a receiving repo's handoff rendered
``test_cmd="pixi run test -n auto"`` and a static "2150+" pass floor without
validating either against that repository (its real pytest collection was 88
tests across five files), then xdist ``-n auto`` hit a worker-level
MemoryError while formatting a collection-error traceback followed by an
xdist node-bookkeeping KeyError — a crash with no ``<path>::<test>`` identity
to isolate, which the old guidance had no way to describe.

This file covers ``meridian.handoff.classify_test_gate_result`` (the pure,
reusable post-run classifier) and ``build_item_briefing``'s repository-aware
test_cmd/test_floor rendering. ``meridian/handoff.py``'s xdist-note PROSE
text is covered separately in ``tests/test_goal_xdist_gate_note.py``; the
settings-reading helpers (``_test_cmd_from_settings`` /
``_test_floor_from_settings`` / ``_is_plausible_test_cmd`` /
``_build_test_gate_config_clause``) and full/delta/starter/goal handoff-mode
parity are covered in ``tests/test_cov_handoff.py``.
"""

from __future__ import annotations

from meridian import handoff as h


# ---------------------------------------------------------------------------
# classify_test_gate_result — the five outcome categories.
# ---------------------------------------------------------------------------


def test_classify_normal_test_failure_parses_collected_count_and_runner_version():
    result = h.classify_test_gate_result(
        test_cmd="pixi run test",
        exit_code=1,
        stdout=(
            "platform win32 -- Python 3.12.1, pytest-8.2.0, pluggy-1.4.0\n"
            "collected 88 items\n"
            "3 failed, 85 passed in 4.21s"
        ),
    )
    assert result["category"] == "test_failure"
    assert result["collected_count"] == 88
    assert result["runner_version"] == "8.2.0"
    assert result["recommend_serial_fallback"] is False
    assert result["isolatable_test_id"] is None
    # The configured command is preserved verbatim on the result.
    assert result["test_cmd"] == "pixi run test"


def test_classify_all_pass_is_also_test_failure_category():
    """'test_failure' is the everyday-outcome category name, not a literal
    claim that something failed -- an all-pass run classifies the same way
    (no infra crash, no collection error) since there is nothing else to do
    but report the normal result."""
    result = h.classify_test_gate_result(
        test_cmd="pixi run test", exit_code=0, stdout="collected 12 items\n12 passed",
    )
    assert result["category"] == "test_failure"
    assert result["exit_code"] == 0


def test_classify_collection_failure_lists_errored_files():
    """The Wave 16 report's '3 known collection errors' scenario: pytest
    starts, reports collection errors against named files, but no worker
    dies. These are real, actionable diagnostics -- list them."""
    stdout = (
        "collected 85 items / 3 errors\n"
        "ERROR tests/test_broken_a.py\n"
        "ERROR tests/test_broken_b.py\n"
        "ERROR tests/test_broken_c.py\n"
        "3 errors during collection"
    )
    result = h.classify_test_gate_result(test_cmd="pixi run test", exit_code=2, stdout=stdout)
    assert result["category"] == "collection_failure"
    assert result["errored_files"] == [
        "tests/test_broken_a.py", "tests/test_broken_b.py", "tests/test_broken_c.py",
    ]
    assert result["recommend_serial_fallback"] is False


def test_classify_worker_crash_with_no_test_identity_recommends_serial_fallback_only():
    """The EXACT Wave 16 scenario: a MemoryError while xdist formats a
    collection-error traceback carries no '<path>::<test>' identity at all.
    The classifier must not invent one -- isolatable_test_id stays None, and
    the caller is pointed straight at the serial fallback."""
    stdout = (
        "INTERNALERROR> Traceback (most recent call last):\n"
        "INTERNALERROR>   File \".../xdist/remote.py\", line 1, in ...\n"
        "INTERNALERROR> MemoryError\n"
    )
    result = h.classify_test_gate_result(
        test_cmd="pixi run test -n auto", exit_code=3, stdout=stdout,
    )
    assert result["category"] == "worker_crash"
    assert result["recommend_serial_fallback"] is True
    assert result["isolatable_test_id"] is None
    assert result["serial_fallback_cmd"] == "pixi run test"


def test_classify_worker_crash_node_bookkeeping_keyerror_no_identity():
    """The second half of the Wave 16 report: an xdist node-bookkeeping
    KeyError after the MemoryError, still with no test identity."""
    stdout = (
        "INTERNALERROR> Traceback (most recent call last):\n"
        "INTERNALERROR>   File \".../xdist/dsession.py\", line 1, in ...\n"
        "INTERNALERROR> KeyError: 'gw2'\n"
    )
    result = h.classify_test_gate_result(
        test_cmd="pixi run test -n auto", exit_code=3, stdout=stdout,
    )
    assert result["category"] == "worker_crash"
    assert result["recommend_serial_fallback"] is True
    assert result["isolatable_test_id"] is None


def test_classify_worker_crash_with_test_identity_is_isolatable():
    """When xdist DOES manage to attribute the crash to a specific worker/
    test (the AssertionError tuple form), that id is surfaced -- but the
    serial fallback is still recommended either way."""
    stdout = (
        "INTERNALERROR> AssertionError: "
        "('tests/test_foo.py::test_bar', <WorkerController gw3>)"
    )
    result = h.classify_test_gate_result(
        test_cmd="pixi run test -n auto", exit_code=3, stdout=stdout,
    )
    assert result["category"] == "worker_crash"
    assert result["isolatable_test_id"] == "tests/test_foo.py::test_bar"
    assert result["recommend_serial_fallback"] is True


def test_classify_timeout_preserves_none_exit_code():
    result = h.classify_test_gate_result(
        test_cmd="pixi run test", exit_code=None, timed_out=True,
    )
    assert result["category"] == "timeout"
    assert result["exit_code"] is None
    assert result["recommend_serial_fallback"] is False


def test_classify_missing_command_explicit_flag():
    """The 'missing legacy script' scenario from the source thesis repo:
    pixi.toml's test task targeted a script that no longer existed. The
    caller (which actually ran the command) reports command_found=False."""
    result = h.classify_test_gate_result(
        test_cmd="pixi run test", exit_code=127, command_found=False,
    )
    assert result["category"] == "missing_command"
    assert result["command_valid"] is False
    # No tests ran -- never mistaken for a normal failure.
    assert result["collected_count"] is None


def test_classify_missing_command_autodetected_from_output():
    """When the caller doesn't pre-classify (command_found left at its
    default), a shell-level 'not found' style message in the captured
    output is still enough to classify as missing_command, not a crash or
    a plain test failure."""
    result = h.classify_test_gate_result(
        test_cmd="pixi run test",
        exit_code=127,
        stderr="error: could not find task `test` in pixi.toml",
    )
    assert result["category"] == "missing_command"
    assert result["command_valid"] is False


def test_classify_command_found_true_overrides_text_heuristics():
    """An explicit command_found=True is authoritative -- even if the output
    happens to contain a phrase that would otherwise trigger the
    missing-command heuristic (e.g. a test asserting error-message text)."""
    result = h.classify_test_gate_result(
        test_cmd="pixi run test",
        exit_code=1,
        stdout="1 failed: AssertionError: expected 'command not found' in output",
        command_found=True,
    )
    assert result["category"] != "missing_command"


def test_classify_exit_code_is_always_preserved_verbatim():
    """abac2298 acceptance: exit-code preservation. The classifier must
    never transform, clamp, or discard the caller-supplied exit code,
    across every category."""
    for code in (0, 1, 2, 127, 137, -9, None):
        result = h.classify_test_gate_result(
            test_cmd="pixi run test", exit_code=code, timed_out=(code is None),
        )
        assert result["exit_code"] == code


def test_classify_test_cmd_preserved_verbatim():
    weird_cmd = "pixi run test  -n auto   --maxfail=1"
    result = h.classify_test_gate_result(test_cmd=weird_cmd, exit_code=0)
    assert result["test_cmd"] == weird_cmd
    # The serial fallback is DERIVED from the same command, not a separate
    # independently-typed default.
    assert result["serial_fallback_cmd"] == h._strip_parallelism_flag(weird_cmd)


def test_classify_never_raises_on_empty_or_garbage_output():
    result = h.classify_test_gate_result(test_cmd="", exit_code=None)
    assert result["category"] in {
        "test_failure", "collection_failure", "worker_crash", "timeout", "missing_command",
    }


# ---------------------------------------------------------------------------
# build_item_briefing — repository-aware per-item test gate (mirrors
# _build_quick_start_goal's fix, symbol-pinned in this item's
# touches_resources).
# ---------------------------------------------------------------------------


def test_build_item_briefing_default_never_invents_a_pass_count():
    """No test_floor passed (the default) must NOT render Meridian's own
    historical '2150+' or any other invented number."""
    brief = h.build_item_briefing({"id": "i1", "title": "Ship it"})
    assert "2150" not in brief
    assert "no test floor is configured" in brief
    assert "--collect-only -q" in brief


def test_build_item_briefing_honors_configured_test_cmd():
    """abac2298 — build_item_briefing used to hardcode the literal string
    'pixi run test' regardless of a caller-resolved test_cmd (it had no
    test_cmd parameter at all). It must now render the ACTUAL configured
    command, mirroring _build_quick_start_goal."""
    brief = h.build_item_briefing(
        {"id": "i1", "title": "Ship it"}, test_cmd="pixi run test-pg -n auto", test_floor=10,
    )
    assert "pixi run test-pg -n auto passes 10+" in brief
    assert "pixi run test passes" not in brief


def test_build_item_briefing_explicit_floor_still_renders_literal_number():
    """Backward-compat: an explicit positive test_floor (e.g. a caller-
    resolved executor_config.test_min) still renders the literal 'N+' claim,
    byte-for-byte the same shape as before this item."""
    brief = h.build_item_briefing({"id": "i1", "title": "Ship it"}, test_floor=250)
    assert "pixi run test passes 250+" in brief


def test_build_item_briefing_blank_test_cmd_falls_back_to_default():
    brief = h.build_item_briefing({"id": "i1", "title": "Ship it"}, test_cmd="   ")
    assert "pixi run test passes" in brief


# ---------------------------------------------------------------------------
# _render_test_floor_clause / classify_test_gate_result agree on the SAME
# serial fallback command as _strip_parallelism_flag -- never two
# independently-derived fallback strings.
# ---------------------------------------------------------------------------


def test_serial_fallback_command_is_single_source_of_truth():
    cmd = "pixi run test -n auto -q"
    goal = h._build_quick_start_goal(
        [{"id": "c1", "title": "FEAT: work", "version": "v1"}],
        test_cmd=cmd, version="v1",
    )
    classified = h.classify_test_gate_result(test_cmd=cmd, exit_code=3, stdout="INTERNALERROR>")
    stripped = h._strip_parallelism_flag(cmd)
    assert f'serial_fallback_cmd="{stripped}"' in goal
    assert classified["serial_fallback_cmd"] == stripped
