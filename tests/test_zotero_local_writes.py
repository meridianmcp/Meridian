"""Tests for the Zotero bridge's open local writes setting (meridian/zotero_local_writes.py) and its wiring.

Every profile folder is a tmp_path. Nothing here reads the real Zotero profile, starts or closes Zotero, runs a
process, or connects to the bridge port (23125): the process calls are blocked, the close and start helpers fail the
test if they are reached, and the socket layer fails the test if anything tries to connect. The restart helper is
injected or replaced wherever a test reaches it.
"""

from __future__ import annotations

import hashlib
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from meridian import tray_main as tray
from meridian import zotero_bridge_api as bridge
from meridian import zotero_launcher
from meridian import zotero_local_writes as zlw
from meridian import zotero_plugin_install as zpi

PREF = "extensions.zotero.meridianZoteroBridge.localWritesOpen"
MARK = (
    "// Meridian: local writes open (set by python -m meridian zotero local-writes). "
    "Off means the bridge asks at the desktop for each write."
)
PREF_TRUE = f'user_pref("{PREF}", true);'
PREF_FALSE = f'user_pref("{PREF}", false);'
ENV_DATA_DIR = "MERIDIAN_ZOTERO_DATA_DIR"
ENV_STATE_DIR = "MERIDIAN_LOCAL_RUNNER_STATE_DIR"
XPI_NAME = "meridian-zotero@usemeridian.us.xpi"
REPO_ROOT = Path(zlw.__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Every default path lives in tmp_path. No real profile is found, and no real Zotero, process or socket is used."""
    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path / "no-zotero-root"))
    monkeypatch.setenv(ENV_STATE_DIR, str(tmp_path / "meridian-state"))

    def _blocked_process(*args, **kwargs):
        # pytest.fail, not a raise: zotero_process_state swallows ordinary exceptions and reports "unknown", so a test
        # that reached the process check would pass silently.
        pytest.fail("a real process call was reached from this test; process calls are blocked in tests")

    def _blocked_socket(*args, **kwargs):
        pytest.fail("the bridge port must not be contacted from this test")

    def _close_not_expected(**kwargs):
        pytest.fail("the close must not run in this test")

    def _start_not_expected(**kwargs):
        pytest.fail("Zotero must not be started in this test")

    monkeypatch.setattr(zpi.subprocess, "run", _blocked_process)
    monkeypatch.setattr(zpi.subprocess, "Popen", _blocked_process)
    monkeypatch.setattr(socket, "create_connection", _blocked_socket)
    monkeypatch.setattr(zpi, "close_zotero_gracefully", _close_not_expected)
    monkeypatch.setattr(zotero_launcher, "start_zotero", _start_not_expected)
    monkeypatch.setattr(bridge, "_restrict_to_owner", lambda target: None)


def _profile(tmp_path: Path, name: str = "profile", *, user_js: "bytes | str | None" = None) -> Path:
    """A Zotero-looking profile folder: it has a prefs.js. ``user_js`` is written as given, when it is not None."""
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "prefs.js").write_text('user_pref("keep", true);\n', encoding="utf-8")
    if user_js is not None:
        (folder / "user.js").write_bytes(user_js.encode("utf-8") if isinstance(user_js, str) else user_js)
    return folder


def _snapshot(folder: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(folder.iterdir()) if path.is_file()}


# ---------------------------------------------------------------------------
# The constants and the shipped default
# ---------------------------------------------------------------------------


def test_the_pref_and_the_mark_match_the_spec():
    assert zlw.PREF == PREF
    assert zlw.MARK == MARK


def test_the_shipped_plugin_default_stays_false():
    text = (REPO_ROOT / "extensions" / "zotero-bridge" / "prefs.js").read_text(encoding="utf-8")
    assert f'pref("{PREF}", false);' in text


def test_user_js_path_is_the_profile_user_js(tmp_path):
    assert zlw.user_js_path(tmp_path) == tmp_path / "user.js"


# ---------------------------------------------------------------------------
# Reading the state
# ---------------------------------------------------------------------------


def test_read_state_is_unset_without_a_user_js_and_for_a_missing_folder(tmp_path):
    folder = _profile(tmp_path)
    assert zlw.read_state(folder) == "unset"
    assert zlw.read_state(tmp_path / "does-not-exist") == "unset"


def test_read_state_reads_true_and_false(tmp_path):
    assert zlw.read_state(_profile(tmp_path, "on", user_js=PREF_TRUE + "\n")) == "on"
    assert zlw.read_state(_profile(tmp_path, "off", user_js=PREF_FALSE + "\n")) == "off"


def test_read_state_last_line_wins(tmp_path):
    on_then_off = _profile(tmp_path, "on-then-off", user_js=f"{PREF_TRUE}\n{PREF_FALSE}\n")
    off_then_on = _profile(tmp_path, "off-then-on", user_js=f"{PREF_FALSE}\n{PREF_TRUE}\n")
    assert zlw.read_state(on_then_off) == "off"
    assert zlw.read_state(off_then_on) == "on"


def test_read_state_accepts_spaces_and_tabs_inside_one_statement(tmp_path):
    folder = _profile(tmp_path, user_js=f'   user_pref( "{PREF}" , true ) ;  \n')
    assert zlw.read_state(folder) == "on"
    folder = _profile(tmp_path, "tabs", user_js=f'\tuser_pref("{PREF}",false);\n')
    assert zlw.read_state(folder) == "off"


@pytest.mark.parametrize(
    "line",
    [f"// {PREF_TRUE}", f'user_pref("{PREF}", 1);', f'user_pref("{PREF}", "true");', f'user_pref("{PREF}", false)'],
)
def test_read_state_refuses_a_pref_line_that_is_not_one_boolean_statement(tmp_path, line):
    folder = _profile(tmp_path, user_js=line + "\n")
    with pytest.raises(ValueError, match="cannot read safely"):
        zlw.read_state(folder)


def test_read_state_on_a_valid_file_returns_on_off_or_unset(tmp_path):
    assert zlw.read_state(_profile(tmp_path, "unset-file", user_js='user_pref("other.pref", 1);\n')) == "unset"
    assert zlw.read_state(_profile(tmp_path, "on-file", user_js=PREF_TRUE + "\n")) == "on"
    assert zlw.read_state(_profile(tmp_path, "off-file", user_js=PREF_FALSE + "\n")) == "off"


SPLIT_TEXTS = [
    f'user_pref("{PREF}",\n  true);\n',
    f'user_pref(\n"{PREF}", true);\n',
    f'user_pref("{PREF}", true\n);\n',
]


@pytest.mark.parametrize("user_js", SPLIT_TEXTS)
def test_read_state_refuses_a_split_pref_statement(tmp_path, user_js):
    folder = _profile(tmp_path, user_js=user_js)
    with pytest.raises(ValueError, match="cannot read safely"):
        zlw.read_state(folder)


def test_read_state_reads_a_latin1_byte_without_failing(tmp_path):
    folder = _profile(tmp_path, user_js=b"// caf\xe9\n" + PREF_TRUE.encode() + b"\n")
    assert zlw.read_state(folder) == "on"


BLOCK_COMMENT_TEXTS = [
    f"/*\n{PREF_TRUE}\n*/\n",
    f"/* {PREF_FALSE} */\n",
    f"// header\n/* a note\n{PREF_TRUE}\nend */\n",
]


@pytest.mark.parametrize("user_js", BLOCK_COMMENT_TEXTS)
def test_read_state_refuses_a_statement_inside_a_block_comment(tmp_path, user_js):
    folder = _profile(tmp_path, user_js=user_js)
    with pytest.raises(ValueError, match="cannot read safely"):
        zlw.read_state(folder)


@pytest.mark.parametrize(
    "user_js",
    [
        f"/*\nnote\n*/\n{PREF_TRUE}\n",
        f"// see /* here\n{PREF_TRUE}\n",
        f'user_pref("other.pref", "/*");\n{PREF_TRUE}\n',
        f'user_pref("glob", "*/*");\n{PREF_TRUE}\n',
        f'user_pref("quoted", "say \\"/*\\"");\n{PREF_TRUE}\n',
    ],
)
def test_read_state_reads_a_statement_that_is_live_code_after_comment_like_text(tmp_path, user_js):
    folder = _profile(tmp_path, user_js=user_js)
    assert zlw.read_state(folder) == "on"


def test_state_text_names_each_state_and_treats_others_as_unknown():
    assert zlw.state_text("on") == "on"
    assert zlw.state_text("off") == "off"
    assert zlw.state_text("unset") == "not set (off by default)"
    assert zlw.state_text("unknown") == "unknown"
    assert zlw.state_text("nonsense") == "unknown"


# ---------------------------------------------------------------------------
# Setting the state
# ---------------------------------------------------------------------------


def test_set_on_writes_the_mark_and_the_pref_to_a_fresh_user_js(tmp_path):
    folder = _profile(tmp_path)
    result = zlw.set_local_writes(folder, True)
    assert result == {"state": "unset", "after": "on", "changed": True}
    assert (folder / "user.js").read_bytes() == f"{MARK}\n{PREF_TRUE}\n".encode("utf-8")
    assert zlw.read_state(folder) == "on"
    assert (folder / "prefs.js").read_text(encoding="utf-8") == 'user_pref("keep", true);\n'


def test_set_off_after_on_round_trips(tmp_path):
    folder = _profile(tmp_path)
    zlw.set_local_writes(folder, True)
    result = zlw.set_local_writes(folder, False)
    assert result == {"state": "on", "after": "off", "changed": True}
    assert zlw.read_state(folder) == "off"
    assert (folder / "user.js").read_bytes().count(MARK.encode("utf-8")) == 1


def test_set_creates_user_js_only_when_the_profile_has_prefs_js(tmp_path):
    with_prefs = _profile(tmp_path, "with-prefs")
    zlw.set_local_writes(with_prefs, True)
    assert (with_prefs / "user.js").is_file()

    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "zotero.sqlite").write_bytes(b"not a prefs file")
    with pytest.raises(ValueError, match="prefs.js"):
        zlw.set_local_writes(bare, True)
    assert not (bare / "user.js").exists()
    assert sorted(path.name for path in bare.iterdir()) == ["zotero.sqlite"]


def test_set_is_idempotent_and_the_second_call_changes_nothing(tmp_path):
    folder = _profile(tmp_path)
    zlw.set_local_writes(folder, True)
    before = _snapshot(folder)
    result = zlw.set_local_writes(folder, True)
    assert result == {"state": "on", "after": "on", "changed": False}
    assert _snapshot(folder) == before


def test_set_rewrites_duplicate_pref_lines_down_to_one(tmp_path):
    folder = _profile(tmp_path, user_js=f"{PREF_TRUE}\n{PREF_TRUE}\n")
    result = zlw.set_local_writes(folder, True)
    assert result == {"state": "on", "after": "on", "changed": True}
    text = (folder / "user.js").read_text(encoding="utf-8")
    assert text.count(PREF) == 1
    assert text.count(MARK) == 1


def test_set_keeps_every_other_line_byte_for_byte(tmp_path):
    original = (
        b"// my comment \xe9 with a latin-1 byte\r\n"
        b'user_pref("browser.startup.homepage", "about:blank");\r\n'
        b"\r\n"
        + PREF_FALSE.encode("utf-8") + b"\r\n"
        + b'user_pref("extensions.startupScanScopes", 1);\r\n'
        + b'user_pref("last.kept", 2)'
    )
    folder = _profile(tmp_path, user_js=original)
    zlw.set_local_writes(folder, True)
    expected = (
        b"// my comment \xe9 with a latin-1 byte\r\n"
        b'user_pref("browser.startup.homepage", "about:blank");\r\n'
        b"\r\n"
        + b'user_pref("extensions.startupScanScopes", 1);\r\n'
        + b'user_pref("last.kept", 2)\r\n'
        + MARK.encode("utf-8") + b"\r\n"
        + PREF_TRUE.encode("utf-8") + b"\r\n"
    )
    assert (folder / "user.js").read_bytes() == expected


def test_set_uses_lf_for_a_file_that_has_no_crlf(tmp_path):
    folder = _profile(tmp_path, user_js='user_pref("a", 1);\n')
    zlw.set_local_writes(folder, False)
    assert (folder / "user.js").read_bytes() == f'user_pref("a", 1);\n{MARK}\n{PREF_FALSE}\n'.encode("utf-8")


@pytest.mark.parametrize("user_js", SPLIT_TEXTS)
def test_set_refuses_a_split_pref_statement_and_leaves_the_file_unchanged(tmp_path, user_js):
    folder = _profile(tmp_path, user_js=user_js)
    before = _snapshot(folder)
    with pytest.raises(ValueError, match="cannot read safely"):
        zlw.set_local_writes(folder, False)
    with pytest.raises(ValueError, match="cannot read safely"):
        zlw.plan_local_writes(folder, False)
    assert _snapshot(folder) == before


@pytest.mark.parametrize("enabled", [True, False])
def test_set_refuses_two_statements_on_one_line_and_leaves_the_file_unchanged(tmp_path, enabled):
    folder = _profile(tmp_path, user_js=f"{PREF_TRUE} {PREF_FALSE}\n")
    before = _snapshot(folder)
    with pytest.raises(ValueError, match="cannot read safely"):
        zlw.set_local_writes(folder, enabled)
    assert _snapshot(folder) == before


@pytest.mark.parametrize("user_js", BLOCK_COMMENT_TEXTS)
@pytest.mark.parametrize("enabled", [True, False])
def test_set_refuses_a_statement_inside_a_block_comment_and_leaves_the_file_unchanged(tmp_path, user_js, enabled):
    folder = _profile(tmp_path, user_js=user_js)
    before = _snapshot(folder)
    with pytest.raises(ValueError, match="cannot read safely"):
        zlw.set_local_writes(folder, enabled)
    with pytest.raises(ValueError, match="cannot read safely"):
        zlw.plan_local_writes(folder, enabled)
    assert _snapshot(folder) == before


def test_a_clean_single_line_is_set_and_unset_correctly(tmp_path):
    folder = _profile(tmp_path, user_js=f"{PREF_FALSE}\n")
    assert zlw.set_local_writes(folder, True) == {"state": "off", "after": "on", "changed": True}
    assert (folder / "user.js").read_bytes() == f"{MARK}\n{PREF_TRUE}\n".encode("utf-8")
    assert zlw.set_local_writes(folder, False) == {"state": "on", "after": "off", "changed": True}
    assert zlw.read_state(folder) == "off"
    assert zlw.set_local_writes(folder, False) == {"state": "off", "after": "off", "changed": False}


def test_set_refuses_a_folder_without_prefs_js_and_writes_nothing(tmp_path):
    folder = tmp_path / "no-prefs"
    folder.mkdir()
    (folder / "user.js").write_bytes(b'user_pref("a", 1);\n')
    before = _snapshot(folder)
    with pytest.raises(ValueError, match="not a Zotero profile"):
        zlw.set_local_writes(folder, True)
    assert _snapshot(folder) == before


def test_set_refuses_a_folder_that_does_not_exist(tmp_path):
    with pytest.raises(ValueError, match="No Zotero profile folder"):
        zlw.set_local_writes(tmp_path / "missing", True)
    assert not (tmp_path / "missing").exists()


def test_a_failed_replace_leaves_the_original_user_js_intact(tmp_path, monkeypatch):
    original = b'user_pref("keep", true);\n'
    folder = _profile(tmp_path, user_js=original)

    def _replace_fails(source, target):
        raise OSError("simulated failure during the replace")

    monkeypatch.setattr(zlw.os, "replace", _replace_fails)
    with pytest.raises(OSError, match="simulated failure"):
        zlw.set_local_writes(folder, True)
    assert (folder / "user.js").read_bytes() == original
    assert [path.name for path in folder.iterdir() if path.name.endswith(".tmp")] == []


def test_plan_reports_the_change_without_writing(tmp_path):
    folder = _profile(tmp_path)
    before = _snapshot(folder)
    assert zlw.plan_local_writes(folder, True) == {"state": "unset", "after": "on", "changed": True}
    assert _snapshot(folder) == before
    zlw.set_local_writes(folder, True)
    assert zlw.plan_local_writes(folder, True) == {"state": "on", "after": "on", "changed": False}


def test_plan_refuses_a_bad_profile_like_set_does(tmp_path):
    with pytest.raises(ValueError):
        zlw.plan_local_writes(tmp_path / "missing", True)


# ---------------------------------------------------------------------------
# Restarting Zotero (the close and start helpers are injected or replaced)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("close_result", "should_start"),
    [("closed", True), ("not_running", True), ("close_failed", False), ("unsupported", False), ("odd", False)],
)
def test_restart_starts_only_after_a_close_that_stopped_zotero(close_result, should_start):
    started = []

    def _start():
        started.append(True)
        return {"action": "started", "exe": "Z", "api_ready": True}

    restarted, text = zlw.restart_zotero(close=lambda: close_result, start=_start)
    assert bool(started) is should_start
    assert restarted is should_start
    if not should_start:
        assert "not" in text.lower()


def test_restart_closed_then_started_says_the_setting_applies():
    order = []

    def _close():
        order.append("close")
        return "closed"

    def _start():
        order.append("start")
        return {"action": "started"}

    restarted, text = zlw.restart_zotero(close=_close, start=_start)
    assert order == ["close", "start"]
    assert restarted is True
    assert text == "Zotero closed and was started again, so the setting applies."


def test_restart_not_running_is_started():
    restarted, text = zlw.restart_zotero(close=lambda: "not_running", start=lambda: {"action": "started"})
    assert restarted is True
    assert text == "Zotero was not running, so it was started. The setting applies."


def test_restart_close_failed_never_starts_and_says_so():
    restarted, text = zlw.restart_zotero(close=lambda: "close_failed", start=lambda: pytest.fail("start must not run"))
    assert restarted is False
    assert text.startswith("Zotero did not close, so it was not started again.")


def test_restart_unsupported_never_starts_and_says_so():
    restarted, text = zlw.restart_zotero(close=lambda: "unsupported", start=lambda: pytest.fail("start must not run"))
    assert restarted is False
    assert "cannot close Zotero" in text


def test_restart_a_close_that_raises_is_a_close_that_failed():
    def _close():
        raise RuntimeError("the close broke")

    restarted, text = zlw.restart_zotero(close=_close, start=lambda: pytest.fail("start must not run"))
    assert restarted is False
    assert "did not close" in text


@pytest.mark.parametrize(
    ("close_result", "action", "expected", "restarted"),
    [
        ("closed", "already_running", "Zotero closed and is running again", True),
        ("closed", "timeout", "its local API has not answered yet", True),
        ("closed", "not_installed", "no Zotero program was found", False),
        ("not_running", "already_running", "Zotero is running, so the setting applies.", True),
        ("not_running", "timeout", "its local API has not answered yet", True),
        ("not_running", "not_installed", "no Zotero program was found", False),
        ("closed", "something_else", "could not be started", False),
    ],
)
def test_restart_reports_each_launch_result(close_result, action, expected, restarted):
    is_restarted, text = zlw.restart_zotero(close=lambda: close_result, start=lambda: {"action": action})
    assert expected in text
    assert is_restarted is restarted


def test_restart_a_launch_that_raises_is_reported_not_raised():
    def _start():
        raise OSError("no launcher")

    restarted, text = zlw.restart_zotero(close=lambda: "closed", start=_start)
    assert restarted is False
    assert "could not be started (no launcher)" in text


def test_restart_accepts_a_bare_action_string_from_the_launch_helper():
    restarted, text = zlw.restart_zotero(close=lambda: "closed", start=lambda: "started")
    assert restarted is True
    assert "so the setting applies" in text


def test_restart_defaults_use_the_installer_close_and_the_launcher_start(monkeypatch):
    seen = {}

    def _close(**kwargs):
        seen["close_kwargs"] = kwargs
        return "closed"

    def _start(**kwargs):
        seen["start_kwargs"] = kwargs
        return {"action": "started", "exe": "Z", "api_ready": True}

    monkeypatch.setattr(zpi, "close_zotero_gracefully", _close)
    monkeypatch.setattr(zotero_launcher, "start_zotero", _start)
    restarted, text = zlw.restart_zotero()
    assert restarted is True
    assert text == "Zotero closed and was started again, so the setting applies."
    assert seen["close_kwargs"] == {}
    # The launcher's pre-launch hook is replaced with nothing, so a restart for this setting installs no plugin.
    assert seen["start_kwargs"]["before_launch"]() is None


# ---------------------------------------------------------------------------
# The default profile (found as the installer finds it)
# ---------------------------------------------------------------------------


def test_default_profile_dir_is_none_without_a_zotero_profile(tmp_path):
    assert zlw.default_profile_dir() is None


def test_default_profile_dir_finds_the_profile_zotero_marks_default(tmp_path, monkeypatch):
    root = tmp_path / "zotero-root"
    folder = root / "Profiles" / "abc.default"
    folder.mkdir(parents=True)
    (folder / "prefs.js").write_text("", encoding="utf-8")
    (root / "profiles.ini").write_text(
        "[Profile0]\nName=default\nIsRelative=1\nPath=Profiles/abc.default\nDefault=1\n", encoding="utf-8",
    )
    monkeypatch.setenv(ENV_DATA_DIR, str(root))
    assert zlw.default_profile_dir() == folder


# ---------------------------------------------------------------------------
# CLI: python -m meridian zotero local-writes status|on|off [--profile PATH] [--restart] [--dry-run]
# ---------------------------------------------------------------------------


@pytest.fixture
def restart_calls(monkeypatch):
    """Replace the restart helper with a recorder, so no test ever closes or starts Zotero."""
    calls = []

    def _fake(**kwargs):
        calls.append(kwargs)
        return True, "Zotero closed and was started again, so the setting applies."

    monkeypatch.setattr(zlw, "restart_zotero", _fake)
    return calls


def test_cli_status_prints_each_state(tmp_path, capsys):
    folder = _profile(tmp_path, "unset")
    assert zlw.cli_main(["status", "--profile", str(folder)]) == 0
    assert "not set (off by default)" in capsys.readouterr().out
    on = _profile(tmp_path, "on", user_js=PREF_TRUE + "\n")
    assert zlw.cli_main(["status", "--profile", str(on)]) == 0
    assert "Local writes are on" in capsys.readouterr().out
    off = _profile(tmp_path, "off", user_js=PREF_FALSE + "\n")
    assert zlw.cli_main(["status", "--profile", str(off)]) == 0
    assert "Local writes are off" in capsys.readouterr().out


def test_cli_status_changes_nothing_and_never_restarts(tmp_path, capsys, restart_calls):
    folder = _profile(tmp_path, user_js=PREF_TRUE + "\n")
    before = _snapshot(folder)
    assert zlw.cli_main(["status", "--profile", str(folder), "--restart", "--dry-run"]) == 0
    assert _snapshot(folder) == before
    assert restart_calls == []


def test_cli_on_writes_and_says_zotero_must_restart(tmp_path, capsys, restart_calls):
    folder = _profile(tmp_path)
    assert zlw.cli_main(["on", "--profile", str(folder)]) == 0
    out = capsys.readouterr().out
    assert "now on" in out
    assert "must restart" in out
    assert "--restart" in out
    assert zlw.read_state(folder) == "on"
    assert restart_calls == []


def test_cli_off_writes_off(tmp_path, capsys, restart_calls):
    folder = _profile(tmp_path, user_js=PREF_TRUE + "\n")
    assert zlw.cli_main(["off", "--profile", str(folder)]) == 0
    assert "now off" in capsys.readouterr().out
    assert zlw.read_state(folder) == "off"
    assert restart_calls == []


def test_cli_on_with_restart_writes_then_restarts_and_prints_the_result(tmp_path, capsys, restart_calls):
    folder = _profile(tmp_path)
    assert zlw.cli_main(["on", "--profile", str(folder), "--restart"]) == 0
    out = capsys.readouterr().out
    assert zlw.read_state(folder) == "on"
    assert len(restart_calls) == 1
    assert "so the setting applies" in out
    assert "must restart" not in out


def test_cli_restart_still_restarts_when_the_file_did_not_change(tmp_path, capsys, restart_calls):
    folder = _profile(tmp_path, user_js=PREF_TRUE + "\n")
    assert zlw.cli_main(["on", "--profile", str(folder), "--restart"]) == 0
    assert "already on" in capsys.readouterr().out
    assert len(restart_calls) == 1


@pytest.mark.parametrize("close_result", ["close_failed", "unsupported"])
def test_cli_restart_exits_1_when_zotero_did_not_close_and_never_starts_it(tmp_path, monkeypatch, close_result):
    folder = _profile(tmp_path)
    started = []

    def _start():
        started.append(True)
        return {"action": "started"}

    monkeypatch.setattr(zlw, "_close_zotero", lambda: close_result)
    monkeypatch.setattr(zlw, "_start_zotero", _start)
    assert zlw.cli_main(["on", "--profile", str(folder), "--restart"]) == 1
    assert started == []
    assert zlw.read_state(folder) == "on"


def test_cli_close_failed_restart_prints_why_zotero_was_not_started(tmp_path, capsys, monkeypatch):
    folder = _profile(tmp_path)
    monkeypatch.setattr(zlw, "_close_zotero", lambda: "close_failed")
    monkeypatch.setattr(zlw, "_start_zotero", lambda: pytest.fail("Zotero must not be started after a failed close"))
    assert zlw.cli_main(["on", "--profile", str(folder), "--restart"]) == 1
    assert "did not close, so it was not started again" in capsys.readouterr().out


def test_cli_restart_exits_0_when_zotero_was_not_running_and_starts_it(tmp_path, capsys, monkeypatch):
    folder = _profile(tmp_path)
    started = []

    def _start():
        started.append(True)
        return {"action": "started", "exe": "Z", "api_ready": True}

    monkeypatch.setattr(zlw, "_close_zotero", lambda: "not_running")
    monkeypatch.setattr(zlw, "_start_zotero", _start)
    assert zlw.cli_main(["on", "--profile", str(folder), "--restart"]) == 0
    assert started == [True]
    assert "Zotero was not running, so it was started. The setting applies." in capsys.readouterr().out


def test_cli_restart_exits_1_and_says_why_when_the_launch_finds_no_zotero_program(tmp_path, capsys, monkeypatch):
    folder = _profile(tmp_path)
    monkeypatch.setattr(zlw, "_close_zotero", lambda: "closed")
    monkeypatch.setattr(zlw, "_start_zotero", lambda: {"action": "not_installed"})
    assert zlw.cli_main(["on", "--profile", str(folder), "--restart"]) == 1
    out = capsys.readouterr().out
    assert "Zotero closed, but no Zotero program was found, so it was not started. Start Zotero yourself." in out
    assert "so the setting applies" not in out
    assert zlw.read_state(folder) == "on"


def test_cli_restart_exits_1_and_says_why_when_the_launch_raises_after_a_clean_close(tmp_path, capsys, monkeypatch):
    folder = _profile(tmp_path)

    def _launch_raises():
        raise OSError("launcher exploded")

    monkeypatch.setattr(zlw, "_close_zotero", lambda: "closed")
    monkeypatch.setattr(zlw, "_start_zotero", _launch_raises)
    assert zlw.cli_main(["on", "--profile", str(folder), "--restart"]) == 1
    out = capsys.readouterr().out
    assert "Zotero could not be started (launcher exploded). Start Zotero yourself." in out
    assert "so the setting applies" not in out
    assert zlw.read_state(folder) == "on"


def test_cli_restart_exits_0_when_zotero_closed_and_its_local_api_has_not_answered_yet(tmp_path, capsys, monkeypatch):
    folder = _profile(tmp_path)
    monkeypatch.setattr(zlw, "_close_zotero", lambda: "closed")
    monkeypatch.setattr(zlw, "_start_zotero", lambda: {"action": "timeout", "exe": "Z", "api_ready": False})
    assert zlw.cli_main(["on", "--profile", str(folder), "--restart"]) == 0
    out = capsys.readouterr().out
    assert "Zotero closed and was started again, but its local API has not answered yet." in out
    assert "so the setting applies" not in out
    assert zlw.read_state(folder) == "on"


def test_cli_restart_exits_0_when_zotero_was_not_running_and_its_local_api_has_not_answered_yet(
    tmp_path, capsys, monkeypatch,
):
    folder = _profile(tmp_path)
    monkeypatch.setattr(zlw, "_close_zotero", lambda: "not_running")
    monkeypatch.setattr(zlw, "_start_zotero", lambda: {"action": "timeout", "exe": "Z", "api_ready": False})
    assert zlw.cli_main(["on", "--profile", str(folder), "--restart"]) == 0
    assert "Zotero was started, but its local API has not answered yet." in capsys.readouterr().out
    assert zlw.read_state(folder) == "on"


def test_cli_restart_refuses_a_pref_line_after_a_string_that_a_line_continuation_leaves_open(
    tmp_path, capsys, restart_calls,
):
    folder = _profile(tmp_path, user_js='user_pref("some.other", "a\\\n");\n' + PREF_TRUE + "\n")
    before = _snapshot(folder)
    assert zlw.cli_main(["status", "--profile", str(folder)]) == 1
    assert "cannot read safely" in capsys.readouterr().out
    assert zlw.cli_main(["on", "--profile", str(folder), "--restart"]) == 1
    assert "cannot read safely" in capsys.readouterr().out
    assert _snapshot(folder) == before
    assert restart_calls == []


def test_a_line_continuation_that_does_not_name_the_setting_is_not_refused(tmp_path):
    folder = _profile(tmp_path, user_js='user_pref("some.other", "a\\\n");\n')
    assert zlw.read_state(folder) == "unset"


def test_a_setting_line_before_a_line_continuation_is_still_read(tmp_path):
    folder = _profile(tmp_path, user_js=PREF_TRUE + '\nuser_pref("some.other", "a\\\n");\n')
    assert zlw.read_state(folder) == "on"


def test_a_pref_name_written_with_an_escape_sequence_is_not_recognized_and_reads_as_unset(tmp_path):
    escaped = 'user_pref("extensions.zotero.meridianZoteroBridge.local\\x57ritesOpen", true);\n'
    folder = _profile(tmp_path, user_js=escaped)
    assert zlw.read_state(folder) == "unset"
    assert zlw.cli_main(["status", "--profile", str(folder)]) == 0
    assert zlw.set_local_writes(folder, True)["changed"] is True
    assert zlw.read_state(folder) == "on"
    assert (folder / "user.js").read_text(encoding="utf-8").startswith(escaped)


def test_the_module_and_the_bridge_readme_both_name_the_escape_sequence_limit():
    readme = (REPO_ROOT / "extensions" / "zotero-bridge" / "README.md").read_text(encoding="utf-8")
    assert "escape sequence" in zlw.__doc__
    assert "escape sequence" in readme


def test_cli_restart_refuses_a_statement_inside_a_block_comment_and_restarts_nothing(tmp_path, capsys, restart_calls):
    folder = _profile(tmp_path, user_js=f"/*\n{PREF_TRUE}\n*/\n")
    before = _snapshot(folder)
    assert zlw.cli_main(["on", "--profile", str(folder), "--restart"]) == 1
    assert "cannot read safely" in capsys.readouterr().out
    assert _snapshot(folder) == before
    assert restart_calls == []


def test_cli_restart_refuses_a_split_setting_line_and_restarts_nothing(tmp_path, capsys, restart_calls):
    folder = _profile(tmp_path, user_js=f'user_pref("{PREF}",\n true);\n')
    before = _snapshot(folder)
    assert zlw.cli_main(["on", "--profile", str(folder), "--restart"]) == 1
    assert "cannot read safely" in capsys.readouterr().out
    assert _snapshot(folder) == before
    assert restart_calls == []


def test_cli_status_refuses_a_split_setting_line_and_says_so(tmp_path, capsys):
    folder = _profile(tmp_path, user_js=f'user_pref("{PREF}",\n true);\n')
    assert zlw.cli_main(["status", "--profile", str(folder)]) == 1
    assert "cannot read safely" in capsys.readouterr().out


def test_cli_dry_run_prints_the_plan_and_writes_nothing(tmp_path, capsys, restart_calls):
    folder = _profile(tmp_path)
    before = _snapshot(folder)
    assert zlw.cli_main(["on", "--profile", str(folder), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Dry run" in out
    assert "Would set local writes to on" in out
    assert "--restart" in out
    assert _snapshot(folder) == before
    assert restart_calls == []


def test_cli_dry_run_with_restart_says_it_would_close_and_start_zotero(tmp_path, capsys, restart_calls):
    folder = _profile(tmp_path, user_js=PREF_TRUE + "\n")
    assert zlw.cli_main(["on", "--profile", str(folder), "--dry-run", "--restart"]) == 0
    out = capsys.readouterr().out
    assert "already on" in out
    assert "Would close Zotero gracefully and start it again" in out
    assert restart_calls == []


def test_cli_dry_run_says_when_the_read_fails(tmp_path, capsys, monkeypatch, restart_calls):
    folder = _profile(tmp_path)

    def _plan_fails(profile, enabled):
        raise OSError("cannot read user.js")

    monkeypatch.setattr(zlw, "plan_local_writes", _plan_fails)
    assert zlw.cli_main(["on", "--profile", str(folder), "--dry-run"]) == 1
    assert "could not be read" in capsys.readouterr().out


def test_cli_refuses_a_folder_that_is_not_a_profile_and_writes_nothing(tmp_path, capsys, restart_calls):
    bare = tmp_path / "bare"
    bare.mkdir()
    assert zlw.cli_main(["on", "--profile", str(bare), "--restart"]) == 1
    assert "not a Zotero profile" in capsys.readouterr().out
    assert not (bare / "user.js").exists()
    assert restart_calls == []


def test_cli_with_no_profile_found_says_so(tmp_path, capsys, restart_calls):
    assert zlw.cli_main(["status"]) == 1
    assert "No Zotero profile was found" in capsys.readouterr().out


def test_cli_status_reports_a_read_error(tmp_path, capsys, monkeypatch):
    folder = _profile(tmp_path)

    def _read_fails(profile):
        raise OSError("cannot read")

    monkeypatch.setattr(zlw, "read_state", _read_fails)
    assert zlw.cli_main(["status", "--profile", str(folder)]) == 1
    assert "could not be read" in capsys.readouterr().out


def test_cli_reports_a_write_error_and_restarts_nothing(tmp_path, capsys, monkeypatch, restart_calls):
    folder = _profile(tmp_path)

    def _set_fails(profile, enabled):
        raise OSError("disk full")

    monkeypatch.setattr(zlw, "set_local_writes", _set_fails)
    assert zlw.cli_main(["on", "--profile", str(folder), "--restart"]) == 1
    assert "were not changed" in capsys.readouterr().out
    assert restart_calls == []


def test_cli_rejects_an_unknown_action(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        zlw.cli_main(["maybe", "--profile", str(tmp_path)])
    assert excinfo.value.code == 2


def test_dispatch_routes_zotero_local_writes_to_the_cli(tmp_path, capsys):
    from meridian import __main__ as entry

    folder = _profile(tmp_path, user_js=PREF_TRUE + "\n")
    assert entry._dispatch_subcommand(["zotero", "local-writes", "status", "--profile", str(folder)]) == 0
    assert "Local writes are on" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# install-plugin --local-writes (the flag sets the pref in the same run, after a successful install)
# ---------------------------------------------------------------------------


@pytest.fixture
def signed_install(monkeypatch, tmp_path):
    """Pretend a signed bundle is stored, so the install can run without Mozilla. Returns the profile and the bytes."""
    payload = b"signed plugin bytes for the test"
    signed = tmp_path / "signed" / XPI_NAME
    signed.parent.mkdir(parents=True)
    signed.write_bytes(payload)
    stored = SimpleNamespace(
        signed=True, tampered=False, status="signed", path=signed, sha256=hashlib.sha256(payload).hexdigest(),
    )
    monkeypatch.setattr(zpi, "_stored_with_fetch", lambda: stored)
    monkeypatch.setattr(zpi, "_fetch_approved_if_missing", lambda: None)
    monkeypatch.setattr(zpi, "zotero_process_state", lambda runner=None: False)
    monkeypatch.setattr(zpi, "_api_listening", lambda: False)
    return _profile(tmp_path, "install-profile")


def test_install_with_local_writes_on_sets_the_pref_after_the_install(signed_install, capsys):
    rc = zpi.cli_main(["--profile", str(signed_install), "--local-writes", "on"])
    out = capsys.readouterr().out
    assert rc == 0
    assert (signed_install / "extensions" / XPI_NAME).is_file()
    assert zlw.read_state(signed_install) == "on"
    assert "Local writes are on in user.js" in out
    assert "Before, the setting was not set" in out


def test_install_without_the_flag_leaves_the_pref_alone(signed_install, capsys):
    rc = zpi.cli_main(["--profile", str(signed_install)])
    assert rc == 0
    assert zlw.read_state(signed_install) == "unset"
    assert "Local writes" not in capsys.readouterr().out


def test_install_on_an_already_current_plugin_still_sets_the_pref(signed_install, capsys):
    assert zpi.cli_main(["--profile", str(signed_install)]) == 0
    rc = zpi.cli_main(["--profile", str(signed_install), "--local-writes", "off"])
    assert rc == 0
    assert zlw.read_state(signed_install) == "off"
    assert "Local writes are off in user.js" in capsys.readouterr().out


def test_install_on_an_already_current_plugin_with_the_pref_already_set_says_so(signed_install, capsys):
    assert zpi.cli_main(["--profile", str(signed_install), "--local-writes", "on"]) == 0
    capsys.readouterr()
    assert zpi.cli_main(["--profile", str(signed_install), "--local-writes", "on"]) == 0
    out = capsys.readouterr().out
    assert "already on in user.js" in out
    assert "the setting applies only after Zotero restarts" in out
    assert "Nothing was restarted" in out


def test_install_refused_while_zotero_runs_does_not_set_the_pref(signed_install, capsys, monkeypatch):
    monkeypatch.setattr(zpi, "zotero_process_state", lambda runner=None: True)
    rc = zpi.cli_main(["--profile", str(signed_install), "--keep-zotero-open", "--local-writes", "on"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Local writes were not changed, because the plugin was not installed." in out
    assert not (signed_install / "user.js").exists() or zlw.read_state(signed_install) == "unset"


def test_install_dry_run_with_the_flag_says_what_it_would_do_and_writes_nothing(tmp_path, capsys):
    folder = _profile(tmp_path)
    before = _snapshot(folder)
    rc = zpi.cli_main(["--profile", str(folder), "--dry-run", "--local-writes", "on"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Local writes: would set on in user.js after a successful install" in out
    assert _snapshot(folder) == before


def test_install_dry_run_says_when_the_setting_is_already_in_place(tmp_path, capsys):
    folder = _profile(tmp_path, user_js=PREF_TRUE + "\n")
    assert zpi.cli_main(["--profile", str(folder), "--dry-run", "--local-writes", "on"]) == 0
    assert "already on in user.js, so nothing would change" in capsys.readouterr().out


def test_install_dry_run_says_when_the_profile_is_not_usable(tmp_path, capsys):
    folder = tmp_path / "bare"
    folder.mkdir()
    (folder / "zotero.sqlite").write_bytes(b"x")
    assert zpi.cli_main(["--profile", str(folder), "--dry-run", "--local-writes", "off"]) == 0
    assert "would not be set" in capsys.readouterr().out


def test_install_failed_local_writes_step_is_reported_and_fails_the_run(signed_install, capsys, monkeypatch):
    def _set_fails(profile, enabled):
        raise OSError("disk full")

    monkeypatch.setattr(zlw, "set_local_writes", _set_fails)
    rc = zpi.cli_main(["--profile", str(signed_install), "--local-writes", "on"])
    assert rc == 1
    assert "Local writes were not set: disk full" in capsys.readouterr().out
    assert (signed_install / "extensions" / XPI_NAME).is_file()


def _make_default_profile(monkeypatch, tmp_path, folder):
    """Point the data dir at a profiles.ini that marks ``folder`` as Zotero's default profile."""
    root = tmp_path / "default-root"
    root.mkdir(exist_ok=True)
    (root / "profiles.ini").write_text(
        f"[Profile0]\nName=default\nIsRelative=0\nPath={folder}\nDefault=1\n", encoding="utf-8",
    )
    monkeypatch.setenv(ENV_DATA_DIR, str(root))


def _failing_repair(profile, result):
    return {**result, "status": "failed", "error": "repair broke"}


def test_install_on_an_already_current_plugin_whose_repair_fails_does_not_set_the_pref(
    signed_install, capsys, monkeypatch,
):
    assert zpi.cli_main(["--profile", str(signed_install)]) == 0
    monkeypatch.setattr(zpi, "_repair_profile", _failing_repair)
    rc = zpi.cli_main(["--profile", str(signed_install), "--local-writes", "on"])
    assert rc == 1
    assert "Local writes were not set: the profile repair failed" in capsys.readouterr().out
    assert zlw.read_state(signed_install) == "unset"
    assert zlw.MARK not in (signed_install / "user.js").read_text(encoding="utf-8")


def test_install_on_the_default_profile_whose_repair_fails_sets_no_pref_and_enables_nothing(
    signed_install, capsys, monkeypatch, tmp_path,
):
    assert zpi.cli_main(["--profile", str(signed_install)]) == 0
    _make_default_profile(monkeypatch, tmp_path, signed_install)
    monkeypatch.setattr(zpi, "_repair_profile", _failing_repair)
    monkeypatch.setattr(zpi, "_register_default", lambda *args, **kwargs: pytest.fail("the enable step must not run"))
    rc = zpi.cli_main(["--profile", str(signed_install), "--local-writes", "on"])
    assert rc == 1
    assert "Local writes were not set: the profile repair failed" in capsys.readouterr().out
    assert zlw.read_state(signed_install) == "unset"


def test_install_whose_repair_fails_after_the_copy_says_the_file_was_copied_and_local_writes_were_not_changed(
    signed_install, capsys, monkeypatch,
):
    monkeypatch.setattr(zpi, "_repair_profile", _failing_repair)
    rc = zpi.cli_main(["--profile", str(signed_install), "--local-writes", "on"])
    out = capsys.readouterr().out
    assert rc == 1
    assert (signed_install / "extensions" / XPI_NAME).is_file()
    assert "The plugin file was copied, but local writes were not changed, because the profile repair failed." in out
    assert "because the plugin was not installed" not in out
    assert zlw.read_state(signed_install) == "unset"
    assert not (signed_install / "user.js").exists()


def test_install_on_the_default_profile_writes_the_pref_before_the_enable_step(
    signed_install, capsys, monkeypatch, tmp_path,
):
    assert zpi.cli_main(["--profile", str(signed_install)]) == 0
    _make_default_profile(monkeypatch, tmp_path, signed_install)
    seen = []

    def _register(profile, running, close, launch, timeout_s):
        seen.append(zlw.read_state(profile))
        return {"status": "already_enabled"}

    monkeypatch.setattr(zpi, "_register_default", _register)
    rc = zpi.cli_main(["--profile", str(signed_install), "--local-writes", "on"])
    assert rc == 0
    assert seen == ["on"]
    assert zlw.read_state(signed_install) == "on"


def test_print_local_writes_passes_the_code_through_when_not_asked():
    assert zpi._print_local_writes({"local_writes": {"status": "failed"}}, None, 0) == 0


def test_local_writes_plan_text_for_a_missing_profile():
    assert "no profile was found" in zpi._local_writes_plan_text(None, True)


def test_with_local_writes_is_the_same_callable_when_not_asked():
    def _install():
        return {"status": "installed"}

    assert zpi._with_local_writes(_install, "unused", None) is _install


def test_dispatch_routes_install_plugin_local_writes_flag(tmp_path, capsys):
    from meridian import __main__ as entry

    folder = _profile(tmp_path)
    rc = entry._dispatch_subcommand(
        ["zotero", "install-plugin", "--profile", str(folder), "--dry-run", "--local-writes", "on"],
    )
    assert rc == 0
    assert "Local writes: would set on" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Tray: pure label, checkmark and dialog text
# ---------------------------------------------------------------------------


def test_tray_label_shows_each_state():
    assert tray.zotero_local_writes_label("on") == "Zotero local writes: on (writes need no prompt)"
    assert tray.zotero_local_writes_label("off") == "Zotero local writes: off (the bridge asks at the desktop)"
    assert tray.zotero_local_writes_label("unset") == "Zotero local writes: off (the bridge asks at the desktop)"
    assert tray.zotero_local_writes_label("unknown") == "Zotero local writes: unknown (user.js could not be read)"
    assert tray.zotero_local_writes_label("nonsense") == "Zotero local writes: unknown (user.js could not be read)"


def test_tray_label_says_when_there_is_no_profile():
    assert tray.zotero_local_writes_label(None) == "Zotero local writes: no Zotero profile found"


def test_tray_checkmark_is_on_only_while_the_setting_is_on():
    assert tray.zotero_local_writes_checked("on") is True
    assert tray.zotero_local_writes_checked("off") is False
    assert tray.zotero_local_writes_checked("unset") is False
    assert tray.zotero_local_writes_checked(None) is False


def test_tray_dialog_text_is_the_plain_consent_question():
    assert tray.zotero_local_writes_turn_on_text() == (
        "Any program on this computer that can read the Zotero bridge token can write to your Zotero library "
        "with no prompt while this is on. Turn it on?"
    )
    assert tray.ZOTERO_LOCAL_WRITES_TURN_ON_BUTTON == "Turn on"
    assert tray.ZOTERO_LOCAL_WRITES_CANCEL_BUTTON == "Cancel"


def test_tray_restart_text_and_buttons():
    assert tray.zotero_local_writes_restart_text() == "Restart Zotero now to apply it?"
    assert tray.ZOTERO_LOCAL_WRITES_RESTART_BUTTON == "Restart"
    assert tray.ZOTERO_LOCAL_WRITES_LATER_BUTTON == "Later"


def test_tray_no_profile_text():
    assert "No Zotero profile was found" in tray.zotero_local_writes_no_profile_text()


def test_tray_state_reads_the_default_profile(tmp_path, monkeypatch):
    assert tray._zotero_local_writes_state() is None
    root = tmp_path / "zotero-root"
    folder = root / "Profiles" / "abc.default"
    folder.mkdir(parents=True)
    (folder / "prefs.js").write_text("", encoding="utf-8")
    (folder / "user.js").write_text(PREF_TRUE + "\n", encoding="utf-8")
    (root / "profiles.ini").write_text(
        "[Profile0]\nName=default\nIsRelative=1\nPath=Profiles/abc.default\nDefault=1\n", encoding="utf-8",
    )
    monkeypatch.setenv(ENV_DATA_DIR, str(root))
    assert tray._zotero_local_writes_state() == "on"


def test_tray_state_is_unknown_when_user_js_cannot_be_read(tmp_path, monkeypatch):
    def _read_fails(profile):
        raise OSError("cannot read")

    monkeypatch.setattr(tray.zotero_local_writes, "default_profile_dir", lambda: tmp_path)
    monkeypatch.setattr(tray.zotero_local_writes, "read_state", _read_fails)
    assert tray._zotero_local_writes_state() == "unknown"


# ---------------------------------------------------------------------------
# Tray: the menu action, with the dialogs replaced and the UI queue run in order
# ---------------------------------------------------------------------------


class _UiQueue:
    """Stands in for the tray's dispatcher: callbacks queue up and run in order, on this thread."""

    def __init__(self):
        self.pending = []

    def __call__(self, callback):
        self.pending.append(callback)

    def run_all(self):
        while self.pending:
            self.pending.pop(0)()


@pytest.fixture
def dialogs(monkeypatch):
    """Record the dialogs and answer the questions. Returns the recorder; set ``answers`` to choose each answer."""
    recorder = SimpleNamespace(questions=[], infos=[], errors=[], answers=[])

    def _ask(title, message, primary, secondary, *, parent=None):
        recorder.questions.append((message, primary, secondary))
        return recorder.answers.pop(0) if recorder.answers else False

    monkeypatch.setattr(tray, "_ask_two_choice", _ask)
    monkeypatch.setattr(tray, "_show_info_dialog", lambda title, message, parent=None: recorder.infos.append(message))
    monkeypatch.setattr(tray, "_show_error_dialog", lambda title, message, parent=None: recorder.errors.append(message))
    return recorder


def test_menu_turning_on_asks_first_then_writes_then_offers_a_restart(tmp_path, dialogs):
    folder = _profile(tmp_path)
    dialogs.answers = [True, False]
    queue = _UiQueue()
    tray._toggle_zotero_local_writes_from_menu(queue, profile=folder)
    queue.run_all()
    assert zlw.read_state(folder) == "on"
    assert dialogs.questions[0] == (tray.zotero_local_writes_turn_on_text(), "Turn on", "Cancel")
    assert dialogs.questions[1] == ("Restart Zotero now to apply it?", "Restart", "Later")
    assert dialogs.errors == []


def test_menu_cancelling_the_question_changes_nothing(tmp_path, dialogs):
    folder = _profile(tmp_path)
    before = _snapshot(folder)
    dialogs.answers = [False]
    queue = _UiQueue()
    tray._toggle_zotero_local_writes_from_menu(queue, profile=folder)
    queue.run_all()
    assert _snapshot(folder) == before
    assert len(dialogs.questions) == 1


def test_menu_turning_off_needs_no_question_and_offers_a_restart(tmp_path, dialogs):
    folder = _profile(tmp_path, user_js=PREF_TRUE + "\n")
    dialogs.answers = [False]
    queue = _UiQueue()
    tray._toggle_zotero_local_writes_from_menu(queue, profile=folder)
    queue.run_all()
    assert zlw.read_state(folder) == "off"
    assert len(dialogs.questions) == 1
    assert dialogs.questions[0][0] == "Restart Zotero now to apply it?"


def test_menu_says_when_there_is_no_profile(dialogs):
    queue = _UiQueue()
    tray._toggle_zotero_local_writes_from_menu(queue)
    queue.run_all()
    assert dialogs.infos == [tray.zotero_local_writes_no_profile_text()]


def test_menu_reports_a_profile_it_cannot_read(tmp_path, dialogs):
    folder = _profile(tmp_path)
    (folder / "user.js").mkdir()  # a folder where the file should be cannot be read as a file
    queue = _UiQueue()
    tray._toggle_zotero_local_writes_from_menu(queue, profile=folder)
    queue.run_all()
    assert len(dialogs.errors) == 1
    assert "could not be read" in dialogs.errors[0]
    assert dialogs.questions == []


def test_menu_reports_a_setting_it_cannot_save(tmp_path, dialogs):
    folder = tmp_path / "no-prefs"
    folder.mkdir()
    dialogs.answers = [True]
    queue = _UiQueue()
    tray._toggle_zotero_local_writes_from_menu(queue, profile=folder)
    queue.run_all()
    assert len(dialogs.errors) == 1
    assert "could not be saved" in dialogs.errors[0]
    assert not (folder / "user.js").exists()


def test_menu_survives_an_unexpected_error_without_raising(tmp_path, dialogs, monkeypatch):
    def _broken(profile):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(tray.zotero_local_writes, "read_state", _broken)
    queue = _UiQueue()
    tray._toggle_zotero_local_writes_from_menu(queue, profile=tmp_path)
    queue.run_all()
    assert dialogs.questions == []


def test_menu_reports_a_setting_line_it_cannot_read_and_changes_nothing(tmp_path, dialogs):
    folder = _profile(tmp_path, user_js=f'user_pref("{PREF}",\n true);\n')
    before = _snapshot(folder)
    queue = _UiQueue()
    tray._toggle_zotero_local_writes_from_menu(queue, profile=folder)
    queue.run_all()
    assert len(dialogs.errors) == 1
    assert "could not be read" in dialogs.errors[0]
    assert dialogs.questions == []
    assert _snapshot(folder) == before


def test_restart_result_is_shown_through_the_queue(dialogs, monkeypatch):
    monkeypatch.setattr(tray.zotero_local_writes, "restart_zotero", lambda: (True, "Zotero was started."))
    queue = _UiQueue()
    text = tray._restart_zotero_and_report(queue)
    queue.run_all()
    assert text == "Zotero was started."
    assert dialogs.infos == ["Zotero was started."]


def test_restart_that_raises_is_reported_not_raised(dialogs, monkeypatch):
    def _broken():
        raise RuntimeError("launcher exploded")

    monkeypatch.setattr(tray.zotero_local_writes, "restart_zotero", _broken)
    queue = _UiQueue()
    text = tray._restart_zotero_and_report(queue)
    queue.run_all()
    assert "launcher exploded" in text
    assert dialogs.infos == [text]


def test_restart_answered_yes_starts_the_restart_on_its_own_thread(dialogs, monkeypatch):
    started = []

    class _FakeThread:
        def __init__(self, target, args=(), kwargs=None, daemon=None):
            started.append((target, args, kwargs))

        def start(self):
            return None

    monkeypatch.setattr(tray.threading, "Thread", _FakeThread)
    dialogs.answers = [True]
    tray._offer_zotero_local_writes_restart(_UiQueue(), parent=None)
    assert len(started) == 1
    assert started[0][0] is tray._restart_zotero_and_report


def test_restart_question_that_cannot_be_shown_changes_nothing(monkeypatch):
    def _broken(*args, **kwargs):
        raise RuntimeError("no display")

    monkeypatch.setattr(tray, "_ask_two_choice", _broken)
    tray._offer_zotero_local_writes_restart(_UiQueue(), parent=None)


def test_turn_on_question_that_cannot_be_shown_changes_nothing(tmp_path, monkeypatch):
    def _broken(*args, **kwargs):
        raise RuntimeError("no display")

    monkeypatch.setattr(tray, "_ask_two_choice", _broken)
    folder = _profile(tmp_path)
    queue = _UiQueue()
    tray._toggle_zotero_local_writes_from_menu(queue, profile=folder)
    queue.run_all()
    assert zlw.read_state(folder) == "unset"


def test_menu_item_is_wired_next_to_the_auto_install_toggle():
    source = (REPO_ROOT / "meridian" / "tray_main.py").read_text(encoding="utf-8")
    autoinstall = source.index("_zotero_plugin_autoinstall_text,")
    local_writes = source.index("_zotero_local_writes_text, _toggle_zotero_local_writes")
    close_toggle = source.index("_zotero_plugin_close_text, _toggle_zotero_plugin_close")
    assert autoinstall < local_writes < close_toggle
