"""Tests for meridian_desktop/tools_status.py: which Meridian tools each client has, and which are live.

Every process, probe and status call here is a fake. Nothing starts a process, reads the real settings
file, probes a real port or makes a request. Each settings file lives in pytest's temp dir. This module
imports no Qt, and the test below checks that by reading its source.
"""

import ast
import json
from datetime import datetime
from pathlib import Path

import pytest

from meridian_desktop import local_client, tools_status, tunnel_status
from meridian_desktop.client_setup_runner import CORE_MODULE, CommandOutput
from meridian_desktop.runner_client import ERR_NOT_AVAILABLE, NOT_AVAILABLE_MESSAGE, RunnerResult
from meridian_desktop.settings_store import DEFAULT_LOCAL_URL, Settings, SettingsStore
from meridian_desktop.tools_status import (
    CHECK_FAILED_TEXT,
    CHECKOUT_INVALID_TEXT,
    CHECKOUT_NOT_SET_TEXT,
    ENTRIES_TOOL,
    ENTRY_CHANGED_TEXT,
    ENTRY_IN_PLACE_TEXT,
    ENTRY_MISSING_TEXT,
    NO_PLAN_TEXT,
    NOT_COVERED_TEXT,
    PYTHON_MISSING_TEXT,
    SERVER_NO_ANSWER_TEXT,
    SERVER_NOT_MERIDIAN_TEXT,
    SERVER_NOT_RUNNING_TEXT,
    SERVER_RUNNING_TEXT,
    SERVER_TOOL,
    STATUS_LIVE,
    STATUS_LIVE_UNCHECKED,
    STATUS_NOT_CONFIGURED,
    STATUS_NOT_LIVE,
    STATUS_NOT_RUNNING,
    STATUS_NOT_SET,
    STATUS_UNCHECKED,
    TUNNEL_NOT_CHECKED_TEXT,
    TUNNEL_TOOL,
    UNCHECKED,
    NO,
    NOT_SET,
    NOT_STARTED,
    YES,
    PlanOnlyRefused,
    PlanOnlyRunner,
    ToolRow,
    read_tools_status,
    status_line,
    status_word,
)

REPO = Path(__file__).resolve().parents[1]
FAKE_TOKEN = "sk_meridian_fake-tools-0007"
CLOCK = datetime(2026, 10, 22, 9, 30, 0)


def clock() -> datetime:
    return CLOCK


class FakeRun:
    """Answers each command from a function of its argv, and records every argv it is given."""

    def __init__(self, respond) -> None:
        self._respond = respond
        self.argvs: list[list[str]] = []

    def __call__(self, argv, cwd, timeout) -> CommandOutput:
        self.argvs.append(list(argv))
        return self._respond(list(argv))


class FakeProbe:
    """A health probe that answers one word, and records each address it is asked about."""

    def __init__(self, answer: str = local_client.HEALTH_RUNNING) -> None:
        self.answer = answer
        self.calls: list[str] = []

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def tunnel_call(result: RunnerResult):
    """A status call that answers one RunnerResult and records each scope it is asked for."""
    calls: list[str] = []

    def status(scope: str) -> RunnerResult:
        calls.append(scope)
        return result

    status.calls = calls
    return status


def entry(client: str, path: Path | str, action: str, *, added=(), changed=(), unchanged=()) -> dict:
    return {
        "client": client,
        "path": str(path),
        "action": action,
        "changes": {"added": list(added), "changed": list(changed), "removed": [], "unchanged": list(unchanged)},
        "backup": None,
        "restart_required": False,
    }


def plan_out(*entries: dict) -> CommandOutput:
    return CommandOutput(0, json.dumps({"results": list(entries)}), "")


def core_error(message: str) -> CommandOutput:
    return CommandOutput(1, "", json.dumps({"error": message}))


def responder(per_client: dict, *, whole: CommandOutput | None = None):
    """Answer a plan. ``whole`` answers 'plan --client all'. A per-client value is a result dict or a CommandOutput."""

    def respond(argv: list[str]) -> CommandOutput:
        target = argv[-1]
        if target == "all":
            if whole is not None:
                return whole
            return plan_out(*(value for value in per_client.values() if isinstance(value, dict)))
        value = per_client[target]
        return value if isinstance(value, CommandOutput) else plan_out(value)

    return respond


@pytest.fixture
def core(tmp_path):
    """A folder that looks like a Meridian core checkout, and a python program that exists."""
    checkout = tmp_path / "core"
    (checkout / "meridian").mkdir(parents=True)
    (checkout / "meridian" / "__init__.py").write_text("", encoding="utf-8")
    (checkout / "meridian" / "client_setup.py").write_text("", encoding="utf-8")
    python = tmp_path / "python.exe"
    python.write_text("", encoding="utf-8")
    return checkout, str(python)


def store_with(tmp_path: Path, *, checkout: str = "", python: str = "", local_url: str | None = None) -> SettingsStore:
    store = SettingsStore(tmp_path / "settings" / "settings.json")
    values = {"core_checkout": checkout, "core_python": python}
    if local_url is not None:
        values["local_url"] = local_url
    store.save(Settings(**values))
    return store


def rows_for(status: tools_status.ToolsStatus, client_label: str) -> list[ToolRow]:
    return [row for row in status.rows if row.client == client_label]


def only_row(status: tools_status.ToolsStatus, client_label: str, tool: str) -> ToolRow:
    matches = [row for row in status.rows if row.client == client_label and row.tool == tool]
    assert len(matches) == 1, [(row.client, row.tool) for row in status.rows]
    return matches[0]


def server_row(status: tools_status.ToolsStatus) -> ToolRow:
    return only_row(status, tools_status.THIS_COMPUTER, SERVER_TOOL)


def tunnel_row(status: tools_status.ToolsStatus) -> ToolRow:
    return only_row(status, tools_status.THIS_COMPUTER, TUNNEL_TOOL)


def all_cell_text(status: tools_status.ToolsStatus) -> str:
    return json.dumps([list(row.cells()) for row in status.rows])


def visible_text(status: tools_status.ToolsStatus) -> str:
    """Every row's cells and the status line, as plain text. A path check reads this, never JSON, so backslashes stay as written."""
    lines = [" | ".join(row.cells()) for row in status.rows]
    return "\n".join(lines + [status_line(status)])


# ----------------------------------------------------------- plan only


def test_only_the_core_plan_command_is_ever_run(tmp_path, core):
    checkout, python = core
    store = store_with(tmp_path, checkout=str(checkout), python=python)
    run = FakeRun(
        responder(
            {
                "codex": entry("codex", tmp_path / "codex.toml", "unchanged", unchanged=["meridian-docs"]),
                "claude-desktop": entry("claude-desktop", tmp_path / "desktop.json", "would_write", added=["meridian-docs"]),
            }
        )
    )
    read_tools_status(store, run=run, probe=FakeProbe(), clock=clock)
    assert run.argvs, "the plan should have run"
    for argv in run.argvs:
        assert argv[1:4] == ["-m", CORE_MODULE, "plan"], argv
        assert "--yes" not in argv
        assert "apply" not in argv and "remove" not in argv


def test_the_wrapper_refuses_anything_but_the_plan_before_it_runs(tmp_path):
    seen: list[list[str]] = []

    def inner(argv, cwd, timeout):
        seen.append(list(argv))
        return CommandOutput(0, "", "")

    plan = PlanOnlyRunner(inner)
    with pytest.raises(PlanOnlyRefused):
        plan(["py", "-m", CORE_MODULE, "apply", "--client", "codex", "--yes"], tmp_path, 5)
    with pytest.raises(PlanOnlyRefused):
        plan(["py", "-m", CORE_MODULE, "remove", "--client", "codex"], tmp_path, 5)
    with pytest.raises(PlanOnlyRefused):
        plan(["py", "-m", CORE_MODULE, "plan", "--client", "codex", "--yes"], tmp_path, 5)
    assert seen == []


def test_the_server_and_tunnel_rows_are_still_shown_when_the_core_is_not_set(tmp_path):
    store = store_with(tmp_path)  # core not set, so no plan runs at all
    status = read_tools_status(store, run=FakeRun(responder({})), probe=FakeProbe(), clock=clock)
    assert server_row(status).live == YES
    assert tunnel_row(status).live == UNCHECKED


@pytest.mark.parametrize(
    "argv",
    [
        ["py", "-m", CORE_MODULE, "remove", "--client", "codex"],
        ["py", "-m", CORE_MODULE, "apply", "--client", "codex", "--yes"],
        ["py", "-m", CORE_MODULE, "plan", "--client", "all", "apply"],
        ["py", "-m", CORE_MODULE, "plan", "--client", "codex", "extra"],
        ["py", "-m", CORE_MODULE, "plan", "--client", "codex", "--yes"],
        ["py", "-m", CORE_MODULE, "plan", "--client", "claude-code"],
        ["py", "-m", CORE_MODULE, "plan"],
        ["py", "-m", CORE_MODULE, "plan", "--client", "all", "--client", "codex"],
    ],
)
def test_the_plan_guard_refuses_a_remove_a_trailing_word_or_any_other_list(tmp_path, argv):
    seen: list[list[str]] = []

    def inner(args, cwd, timeout):
        seen.append(list(args))
        return CommandOutput(0, "", "")

    plan = PlanOnlyRunner(inner)
    with pytest.raises(PlanOnlyRefused):
        plan(argv, tmp_path, 5)
    assert seen == []


@pytest.mark.parametrize("target", ["all", "codex", "claude-desktop"])
def test_the_plan_guard_runs_exactly_the_plan_argv_the_module_builds(tmp_path, target):
    seen: list[list[str]] = []

    def inner(args, cwd, timeout):
        seen.append(list(args))
        return CommandOutput(0, "", "")

    plan = PlanOnlyRunner(inner, python="py")
    argv = ["py", "-m", CORE_MODULE, "plan", "--client", target]
    plan(argv, tmp_path, 5)
    assert seen == [argv]
    assert [recorded for recorded, _output in plan.outputs] == [tuple(argv)]


def test_the_plan_guard_refuses_a_program_other_than_the_core_python(tmp_path):
    seen: list[list[str]] = []

    def inner(args, cwd, timeout):
        seen.append(list(args))
        return CommandOutput(0, "", "")

    plan = PlanOnlyRunner(inner, python="py")
    with pytest.raises(PlanOnlyRefused):
        plan(["cmd", "-m", CORE_MODULE, "plan", "--client", "all"], tmp_path, 5)
    assert seen == []


# ----------------------------------------------------------- core not set


def test_unset_core_gives_a_plain_not_set_state_and_no_process_starts(tmp_path):
    store = store_with(tmp_path)
    run = FakeRun(responder({}))
    status = read_tools_status(store, run=run, probe=FakeProbe(), clock=clock)
    assert run.argvs == []
    codex = rows_for(status, "Codex")
    assert len(codex) == 1
    assert codex[0].configured == NOT_SET
    assert codex[0].status == STATUS_NOT_SET
    assert codex[0].cells()[2:5] == ("Not checked", "Not checked", "Not checked")
    assert codex[0].detail == CHECKOUT_NOT_SET_TEXT
    assert status.message == CHECKOUT_NOT_SET_TEXT
    assert all(row.status == "Not checked" for row in rows_for(status, "Codex") + rows_for(status, "Claude Desktop"))
    assert "Not set" not in visible_text(status)


def test_missing_checkout_folder_gives_one_fixed_sentence_and_never_the_path(tmp_path):
    missing = tmp_path / "gone"
    store = store_with(tmp_path, checkout=str(missing), python=str(tmp_path))
    status = read_tools_status(store, run=FakeRun(responder({})), probe=FakeProbe(), clock=clock)
    codex = rows_for(status, "Codex")[0]
    assert codex.status == STATUS_NOT_SET
    assert codex.detail == CHECKOUT_INVALID_TEXT
    assert status.message == CHECKOUT_INVALID_TEXT
    assert str(missing) not in visible_text(status)


def test_a_folder_that_is_not_a_core_checkout_is_invalid_and_hides_the_path(tmp_path):
    folder = tmp_path / "not-core"
    folder.mkdir()
    python = tmp_path / "python.exe"
    python.write_text("", encoding="utf-8")
    store = store_with(tmp_path, checkout=str(folder), python=str(python))
    run = FakeRun(responder({}))
    status = read_tools_status(store, run=run, probe=FakeProbe(), clock=clock)
    assert run.argvs == []
    assert rows_for(status, "Codex")[0].detail == CHECKOUT_INVALID_TEXT
    assert status.message == CHECKOUT_INVALID_TEXT
    assert str(folder) not in visible_text(status)


def test_a_python_program_that_is_missing_says_so_and_hides_the_path(tmp_path, core):
    checkout, _python = core
    gone = tmp_path / "gone" / "python.exe"
    store = store_with(tmp_path, checkout=str(checkout), python=str(gone))
    run = FakeRun(responder({}))
    status = read_tools_status(store, run=run, probe=FakeProbe(), clock=clock)
    assert run.argvs == []
    assert rows_for(status, "Codex")[0].detail == PYTHON_MISSING_TEXT
    assert status.message == PYTHON_MISSING_TEXT
    assert str(gone) not in visible_text(status)


def test_an_unset_python_says_it_is_missing(tmp_path, core):
    checkout, _python = core
    store = store_with(tmp_path, checkout=str(checkout), python="")
    status = read_tools_status(store, run=FakeRun(responder({})), probe=FakeProbe(), clock=clock)
    assert rows_for(status, "Codex")[0].detail == PYTHON_MISSING_TEXT
    assert status.message == PYTHON_MISSING_TEXT


def test_unset_core_still_says_claude_code_is_not_covered(tmp_path):
    store = store_with(tmp_path)
    status = read_tools_status(store, run=FakeRun(responder({})), probe=FakeProbe(), clock=clock)
    claude = rows_for(status, "Claude Code")[0]
    assert claude.configured == UNCHECKED
    assert claude.detail == NOT_COVERED_TEXT.format(label="Claude Code")


# ----------------------------------------------------------- client entries


def test_entries_in_place_are_configured_and_live_is_not_checked(tmp_path, core):
    checkout, python = core
    store = store_with(tmp_path, checkout=str(checkout), python=python)
    run = FakeRun(
        responder({"codex": entry("codex", tmp_path / "codex.toml", "unchanged", unchanged=["meridian-docs", "meridian-outputs"])})
    )
    status = read_tools_status(store, run=run, probe=FakeProbe(), clock=clock)
    codex = rows_for(status, "Codex")
    assert [row.tool for row in codex] == ["meridian-docs", "meridian-outputs"]
    for row in codex:
        assert row.configured == YES
        assert row.live == UNCHECKED
        assert row.status == STATUS_LIVE_UNCHECKED
        assert row.detail == ENTRY_IN_PLACE_TEXT


def test_missing_and_changed_entries_are_not_configured(tmp_path, core):
    checkout, python = core
    store = store_with(tmp_path, checkout=str(checkout), python=python)
    run = FakeRun(
        responder(
            {
                "claude-desktop": entry(
                    "claude-desktop",
                    tmp_path / "desktop.json",
                    "would_write",
                    added=["meridian-search"],
                    changed=["meridian-docs"],
                    unchanged=["meridian-outputs"],
                )
            }
        )
    )
    status = read_tools_status(store, run=run, probe=FakeProbe(), clock=clock)
    desktop = rows_for(status, "Claude Desktop")
    assert [row.tool for row in desktop] == ["meridian-docs", "meridian-outputs", "meridian-search"]
    by_tool = {row.tool: row for row in desktop}
    assert by_tool["meridian-docs"].status == STATUS_NOT_CONFIGURED
    assert by_tool["meridian-docs"].detail == ENTRY_CHANGED_TEXT.format(label="Claude Desktop")
    assert by_tool["meridian-search"].status == STATUS_NOT_CONFIGURED
    assert by_tool["meridian-search"].detail == ENTRY_MISSING_TEXT.format(label="Claude Desktop")
    assert by_tool["meridian-outputs"].status == STATUS_LIVE_UNCHECKED


def test_a_plan_with_no_entry_names_gives_one_row_for_the_client(tmp_path, core):
    checkout, python = core
    store = store_with(tmp_path, checkout=str(checkout), python=python)
    run = FakeRun(responder({"codex": entry("codex", tmp_path / "codex.toml", "unchanged")}))
    status = read_tools_status(store, run=run, probe=FakeProbe(), clock=clock)
    codex = rows_for(status, "Codex")
    assert len(codex) == 1
    assert codex[0].tool == ENTRIES_TOOL
    assert codex[0].configured == YES


def test_a_client_with_nothing_in_place_is_not_configured(tmp_path, core):
    checkout, python = core
    store = store_with(tmp_path, checkout=str(checkout), python=python)
    run = FakeRun(responder({"codex": entry("codex", tmp_path / "codex.toml", "would_write")}))
    status = read_tools_status(store, run=run, probe=FakeProbe(), clock=clock)
    codex = rows_for(status, "Codex")
    assert len(codex) == 1
    assert codex[0].status == STATUS_NOT_CONFIGURED
    assert codex[0].detail == ENTRY_MISSING_TEXT.format(label="Codex")


def test_claude_code_is_never_sent_to_the_core(tmp_path, core):
    checkout, python = core
    store = store_with(tmp_path, checkout=str(checkout), python=python)
    run = FakeRun(responder({"codex": entry("codex", tmp_path / "codex.toml", "unchanged")}))
    read_tools_status(store, run=run, probe=FakeProbe(), clock=clock)
    assert all("claude-code" not in argv for argv in run.argvs)


def test_a_core_error_for_one_client_leaves_the_others_readable(tmp_path, core):
    checkout, python = core
    store = store_with(tmp_path, checkout=str(checkout), python=python)
    run = FakeRun(
        responder(
            {
                "codex": core_error("codex settings file is not readable"),
                "claude-desktop": entry("claude-desktop", tmp_path / "desktop.json", "unchanged", unchanged=["meridian-docs"]),
            },
            whole=core_error("codex settings file is not readable"),
        )
    )
    status = read_tools_status(store, run=run, probe=FakeProbe(), clock=clock)
    codex = rows_for(status, "Codex")
    assert len(codex) == 1
    assert codex[0].configured == UNCHECKED
    assert codex[0].detail == NO_PLAN_TEXT
    assert "codex settings file is not readable" not in visible_text(status)
    desktop = rows_for(status, "Claude Desktop")
    assert [row.tool for row in desktop] == ["meridian-docs"]
    assert desktop[0].configured == YES


def test_a_core_error_that_names_an_absolute_path_never_shows_the_path(tmp_path, core):
    checkout, python = core
    store = store_with(tmp_path, checkout=str(checkout), python=python)
    locked = tmp_path / "codex" / "config.toml"
    message = f"cannot read {locked}: access denied"
    run = FakeRun(
        responder(
            {
                "codex": core_error(message),
                "claude-desktop": entry("claude-desktop", tmp_path / "desktop.json", "unchanged", unchanged=["meridian-docs"]),
            },
            whole=core_error(message),
        )
    )
    status = read_tools_status(store, run=run, probe=FakeProbe(), clock=clock)
    assert rows_for(status, "Codex")[0].detail == NO_PLAN_TEXT
    assert message not in visible_text(status)
    assert str(locked) not in visible_text(status)
    assert str(tmp_path) not in visible_text(status)


def test_a_failed_plan_says_why_and_shows_nothing_as_configured(tmp_path, core):
    checkout, python = core
    store = store_with(tmp_path, checkout=str(checkout), python=python)
    run = FakeRun(lambda argv: CommandOutput(2, "", f"boom {FAKE_TOKEN}"))
    status = read_tools_status(store, run=run, probe=FakeProbe(), clock=clock)
    assert all(row.configured != YES for row in rows_for(status, "Codex") + rows_for(status, "Claude Desktop"))
    assert FAKE_TOKEN not in all_cell_text(status)


def test_the_token_is_never_shown_in_any_row(tmp_path, core):
    checkout, python = core
    store = store_with(tmp_path, checkout=str(checkout), python=python)
    run = FakeRun(responder({"codex": entry("codex", tmp_path / "codex.toml", "unchanged", unchanged=["meridian-docs"])}))
    status = read_tools_status(
        store,
        run=run,
        probe=FakeProbe(RuntimeError(f"leaks {FAKE_TOKEN}")),
        tunnel_status_call=tunnel_call(RunnerResult(verb="status", ok=True, payload={"scope": "tunnel", "child": {"state": "running"}})),
        clock=clock,
    )
    assert FAKE_TOKEN not in all_cell_text(status)


# ----------------------------------------------------------- local server


def test_a_running_local_server_is_live_and_probed_on_the_loopback_address(tmp_path):
    store = store_with(tmp_path)
    probe = FakeProbe(local_client.HEALTH_RUNNING)
    status = read_tools_status(store, run=FakeRun(responder({})), probe=probe, clock=clock)
    row = server_row(status)
    assert (row.configured, row.live) == (YES, YES)
    assert row.status == STATUS_LIVE
    assert row.detail == SERVER_RUNNING_TEXT
    assert probe.calls == [DEFAULT_LOCAL_URL]


@pytest.mark.parametrize(
    ("answer", "detail"),
    [
        (local_client.HEALTH_NOT_RUNNING, SERVER_NOT_RUNNING_TEXT),
        (local_client.HEALTH_NOT_MERIDIAN, SERVER_NOT_MERIDIAN_TEXT),
        (local_client.HEALTH_ERROR, SERVER_NO_ANSWER_TEXT),
    ],
)
def test_a_server_that_is_not_answering_is_configured_but_not_live(tmp_path, answer, detail):
    store = store_with(tmp_path)
    status = read_tools_status(store, run=FakeRun(responder({})), probe=FakeProbe(answer), clock=clock)
    row = server_row(status)
    assert (row.configured, row.live) == (YES, NO)
    assert row.status == STATUS_NOT_LIVE
    assert row.detail == detail


@pytest.mark.parametrize(
    "address",
    ["http://example.com:7878", "https://127.0.0.1:7878", "http://127.0.0.1.example.com:7878", "http://10.0.0.5:7878"],
)
def test_a_non_loopback_address_is_refused_and_never_probed(tmp_path, address):
    store = store_with(tmp_path, local_url=address)
    probe = FakeProbe()
    status = read_tools_status(store, run=FakeRun(responder({})), probe=probe, clock=clock)
    row = server_row(status)
    assert row.configured == NO
    assert row.live == UNCHECKED
    assert row.status == STATUS_NOT_CONFIGURED
    assert probe.calls == []


def test_a_probe_that_raises_is_not_live_and_only_its_class_name_is_shown(tmp_path):
    class ProbeBoom(Exception):
        pass

    store = store_with(tmp_path)
    status = read_tools_status(
        store, run=FakeRun(responder({})), probe=FakeProbe(ProbeBoom(f"secret {FAKE_TOKEN}")), clock=clock
    )
    row = server_row(status)
    assert row.live == NO
    assert "ProbeBoom" in row.detail
    assert FAKE_TOKEN not in all_cell_text(status)


# ----------------------------------------------------------- tunnel


def test_a_running_tunnel_is_live(tmp_path):
    store = store_with(tmp_path)
    status_call = tunnel_call(RunnerResult(verb="status", ok=True, payload={"scope": "tunnel", "child": {"state": "running"}}))
    status = read_tools_status(store, run=FakeRun(responder({})), probe=FakeProbe(), tunnel_status_call=status_call, clock=clock)
    row = tunnel_row(status)
    assert (row.configured, row.live) == (YES, YES)
    assert row.status == STATUS_LIVE
    assert status_call.calls == [tunnel_status.TUNNEL_SCOPE]


def test_a_stopped_tunnel_is_configured_but_not_live_and_says_how_to_start_it_by_hand(tmp_path):
    store = store_with(tmp_path)
    status_call = tunnel_call(RunnerResult(verb="status", ok=True, payload={"scope": "tunnel", "child": {"state": "stopped"}}))
    status = read_tools_status(store, run=FakeRun(responder({})), probe=FakeProbe(), tunnel_status_call=status_call, clock=clock)
    row = tunnel_row(status)
    assert (row.configured, row.live) == (YES, NO)
    assert row.status == STATUS_NOT_LIVE
    assert tunnel_status.START_HINT in row.detail
    assert "codebase-memory" in row.detail


@pytest.mark.parametrize("child", ["stopped", "crashed"])
def test_a_tunnel_that_was_stopped_or_crashed_stays_configured_and_not_live(tmp_path, child):
    store = store_with(tmp_path)
    status_call = tunnel_call(RunnerResult(verb="status", ok=True, payload={"scope": "tunnel", "child": {"state": child}}))
    status = read_tools_status(store, run=FakeRun(responder({})), probe=FakeProbe(), tunnel_status_call=status_call, clock=clock)
    row = tunnel_row(status)
    assert (row.configured, row.live) == (YES, NO)
    assert row.cells()[3] == "Yes"
    assert row.status == STATUS_NOT_LIVE


def test_a_tunnel_the_runner_has_no_record_of_is_not_checked_and_not_running(tmp_path):
    store = store_with(tmp_path)
    status_call = tunnel_call(RunnerResult(verb="status", ok=True, payload={"scope": "tunnel", "child": {"state": "not_started"}}))
    status = read_tools_status(store, run=FakeRun(responder({})), probe=FakeProbe(), tunnel_status_call=status_call, clock=clock)
    row = tunnel_row(status)
    assert (row.configured, row.live) == (NOT_STARTED, NO)
    assert row.cells()[2:5] == (STATUS_NOT_RUNNING, "Not checked", "No")
    assert row.status == "Not running"
    assert tunnel_status.START_HINT in row.detail
    assert "no record of a tunnel process" in row.detail


def test_an_unknown_tunnel_is_never_shown_as_live(tmp_path):
    store = store_with(tmp_path)
    status_call = tunnel_call(RunnerResult(verb="status", ok=False, error_code=ERR_NOT_AVAILABLE, error_message=NOT_AVAILABLE_MESSAGE))
    status = read_tools_status(store, run=FakeRun(responder({})), probe=FakeProbe(), tunnel_status_call=status_call, clock=clock)
    row = tunnel_row(status)
    assert row.live == UNCHECKED
    assert row.status == STATUS_UNCHECKED
    assert tunnel_status.NOT_AVAILABLE_REASON in row.detail


def test_a_tunnel_status_call_that_raises_is_unknown_and_shows_only_the_class_name(tmp_path):
    store = store_with(tmp_path)

    def status_call(scope: str) -> RunnerResult:
        raise OSError(f"path with {FAKE_TOKEN}")

    status = read_tools_status(store, run=FakeRun(responder({})), probe=FakeProbe(), tunnel_status_call=status_call, clock=clock)
    row = tunnel_row(status)
    assert row.status == STATUS_UNCHECKED
    assert "OSError" in row.detail
    assert FAKE_TOKEN not in all_cell_text(status)


def test_no_tunnel_call_wired_means_the_tunnel_is_not_checked(tmp_path):
    store = store_with(tmp_path)
    status = read_tools_status(store, run=FakeRun(responder({})), probe=FakeProbe(), tunnel_status_call=None, clock=clock)
    row = tunnel_row(status)
    assert row.status == STATUS_UNCHECKED
    assert row.detail == TUNNEL_NOT_CHECKED_TEXT


# ----------------------------------------------------------- words and time


@pytest.mark.parametrize(
    ("configured", "live", "word"),
    [
        (YES, YES, STATUS_LIVE),
        (YES, NO, STATUS_NOT_LIVE),
        (YES, UNCHECKED, STATUS_LIVE_UNCHECKED),
        (NO, UNCHECKED, STATUS_NOT_CONFIGURED),
        (NO, NO, STATUS_NOT_CONFIGURED),
        (UNCHECKED, UNCHECKED, STATUS_UNCHECKED),
        (NOT_SET, UNCHECKED, STATUS_NOT_SET),
        (NOT_STARTED, NO, STATUS_NOT_RUNNING),
    ],
)
def test_each_row_state_has_one_status_word(configured, live, word):
    assert status_word(configured, live) == word


def test_the_status_line_gives_the_time_checked(tmp_path):
    store = store_with(tmp_path)
    status = read_tools_status(store, run=FakeRun(responder({})), probe=FakeProbe(), clock=clock)
    assert status.checked_at == CLOCK
    assert status_line(status).endswith("Time checked: 2026-10-22 09:30:00.")


def test_a_status_with_no_check_time_has_no_time_in_its_line():
    assert status_line(tools_status.ToolsStatus(rows=(), message="The tools check failed (ValueError).")) == (
        "The tools check failed (ValueError)."
    )


def test_every_row_is_a_plain_sentence_with_no_placeholder_left(tmp_path, core):
    checkout, python = core
    store = store_with(tmp_path, checkout=str(checkout), python=python)
    run = FakeRun(responder({"codex": entry("codex", tmp_path / "codex.toml", "would_write", added=["meridian-docs"])}))
    status = read_tools_status(store, run=run, probe=FakeProbe(local_client.HEALTH_NOT_RUNNING), clock=clock)
    text = all_cell_text(status)
    assert "{" not in text and "}" not in text
    assert "Not built yet" not in text


def test_a_deeply_nested_plan_reply_never_escapes_the_read(tmp_path, core):
    checkout, python = core
    store = store_with(tmp_path, checkout=str(checkout), python=python)
    deep = "[" * 200_000 + "]" * 200_000
    run = FakeRun(lambda argv: CommandOutput(0, deep, ""))
    status = read_tools_status(store, run=run, probe=FakeProbe(), clock=clock)
    assert "RecursionError" in status.message
    assert server_row(status).live == YES
    assert status.checked_at == CLOCK


def test_a_deeply_nested_settings_file_still_gives_a_normal_status(tmp_path):
    path = tmp_path / "settings" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")
    run = FakeRun(responder({}))
    probe = FakeProbe()
    status = read_tools_status(SettingsStore(path), run=run, probe=probe, clock=clock)
    assert isinstance(status, tools_status.ToolsStatus)
    assert status.message == CHECK_FAILED_TEXT.format(name="RecursionError")
    assert status.checked_at == CLOCK
    assert status.rows
    assert run.argvs == [] and probe.calls == []
    assert server_row(status).live == UNCHECKED
    assert tunnel_row(status).live == UNCHECKED
    assert all(YES not in (row.configured, row.live) for row in status.rows)
    assert str(tmp_path) not in visible_text(status)


def test_a_checked_read_says_no_tool_is_started_or_stopped(tmp_path, core):
    checkout, python = core
    store = store_with(tmp_path, checkout=str(checkout), python=python)
    status = read_tools_status(store, run=FakeRun(responder({})), probe=FakeProbe(), clock=clock)
    assert status.message == "Checked. No tool is started or stopped."
    assert "Nothing was written" not in visible_text(status)


# ----------------------------------------------------------- source


def test_the_copy_names_only_what_the_refresh_does():
    panel = REPO / "meridian_desktop" / "ui" / "tools_panel.py"
    module = REPO / "meridian_desktop" / "tools_status.py"
    texts = {path: path.read_text(encoding="utf-8") for path in (module, panel)}
    banned = (
        "starts no process",
        "starts, stops and installs",
        "starts, stops, installs",
        "Nothing is started",
        "Nothing here starts",
        "nothing is written",
        "nothing was written",
        "changes nothing",
    )
    for path, text in texts.items():
        lowered = text.lower()
        for phrase in banned:
            assert phrase.lower() not in lowered, (path.name, phrase)
    assert "No tool is started or stopped." in texts[panel]


def test_the_module_imports_no_qt():
    tree = ast.parse((REPO / "meridian_desktop" / "tools_status.py").read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    assert not [name for name in names if name.split(".")[0] in {"PySide6", "PyQt5", "PyQt6"}]
