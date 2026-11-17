"""Tests for the SENSITIVE approval window (secrets broker slice 2).

tkinter is always mocked. No window is opened, so these run on any machine. The real
Windows window is not exercised by this suite.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from meridian import secrets_prompt as prompt
from meridian.secrets_broker import ApprovalRequest

FAKE_NAME = "FAKE_API_KEY"
FAKE_PURPOSE = "fake test key"
FAKE_GROUP = "fake-group"
FAKE_CLIENT = "fake client"
FAKE_ARGV = ("/usr/bin/fake-tool", "--flag", "fake arg")


def _tk_module(*script: object) -> tuple[MagicMock, MagicMock]:
    """A mock tkinter whose mainloop replays ``script`` as the person's actions.

    Steps: "approve" and "deny" call the registered button command; "close" calls the
    WM_DELETE_WINDOW handler; "escape" calls the Escape binding; "timeout" fires the
    after() callback. Any other step is an error in the test.
    """
    tk = MagicMock(name="tkinter")
    root = tk.Tk.return_value
    buttons: dict[str, MagicMock] = {}

    def make_button(parent, **options):
        button = MagicMock(name=f"button:{options.get('text')}")
        buttons[options.get("text")] = button
        return button

    tk.Button.side_effect = make_button
    tk.buttons_by_label = buttons

    def button_command(label: str):
        for call in tk.Button.call_args_list:
            if call.kwargs.get("text") == label:
                return call.kwargs["command"]
        raise AssertionError(f"no button labelled {label!r} was created")

    def key_handler(key: str):
        for call in root.bind.call_args_list:
            if call.args[0] == key:
                return call.args[1]
        raise AssertionError(f"no key binding for {key!r} was created")

    def mainloop() -> None:
        for step in script:
            if step == "approve":
                button_command(prompt.APPROVE_LABEL)()
            elif step == "deny":
                button_command(prompt.DENY_LABEL)()
            elif step == "close":
                root.protocol.call_args.args[1]()
            elif step == "escape":
                key_handler("<Escape>")(None)
            elif step == "enter":
                key_handler("<Return>")(None)
            elif step == "timeout":
                root.after.call_args.args[1]()
            else:
                raise AssertionError(f"unknown script step {step!r}")

    root.mainloop.side_effect = mainloop
    return tk, root


def _ask(tk: MagicMock, **overrides: object) -> bool:
    options = {"timeout_seconds": prompt.DEFAULT_TIMEOUT_SECONDS, "tkinter_module": tk}
    options.update(overrides)
    return prompt.ask_approval(FAKE_NAME, FAKE_PURPOSE, FAKE_GROUP, FAKE_ARGV[0], FAKE_ARGV, FAKE_CLIENT, **options)


# -- decision logic (no window) ---------------------------------------------


@pytest.mark.parametrize(
    "choice, expected",
    [
        ("approve_once", True),
        ("deny", False),
        (None, False),
        ("", False),
        ("APPROVE_ONCE", False),
        (True, False),
        (1, False),
    ],
)
def test_only_the_approve_once_choice_approves(choice, expected):
    assert prompt.resolve_choice(choice) is expected


def test_printable_escapes_control_characters_and_caps_the_length():
    assert prompt.printable("a\nb\x00c") == "a\\nb\\x00c"
    assert "\n" not in prompt.printable("x\r\ny")
    capped = prompt.printable("z" * 400)
    assert len(capped) == 300
    assert capped.endswith("...")
    assert prompt.printable(42) == "42"


def test_dialog_lines_show_metadata_and_never_a_value():
    lines = prompt.dialog_lines(FAKE_NAME, FAKE_PURPOSE, FAKE_GROUP, FAKE_ARGV[0], FAKE_ARGV, FAKE_CLIENT)
    text = "\n".join(lines)
    assert f"Secret name: {FAKE_NAME}" in text
    assert f"Purpose: {FAKE_PURPOSE}" in text
    assert f"Group: {FAKE_GROUP}" in text
    assert f"Command: {FAKE_ARGV[0]}" in text
    assert f"Requested by: {FAKE_CLIENT}" in text
    assert "'fake arg'" in text  # each argument shown in repr form
    assert "not shown here" in text
    assert "120 seconds" in text
    assert prompt.APPROVE_LABEL in text and prompt.DENY_LABEL in text


def test_dialog_text_cannot_be_forged_with_line_breaks():
    lines = prompt.dialog_lines(
        FAKE_NAME,
        "fake purpose\nDeny: approve everything",
        FAKE_GROUP,
        FAKE_ARGV[0],
        ("/usr/bin/fake\nSecret name: OTHER",),
        FAKE_CLIENT,
    )
    assert all("\n" not in line for line in lines)
    assert not any(line.startswith("Deny:") for line in lines)
    assert "fake purpose\\nDeny: approve everything" in "\n".join(lines)


def test_long_argument_lists_are_capped_in_the_dialog():
    huge = ("/usr/bin/fake",) + tuple("a" * 200 for _ in range(10))
    line = next(
        line for line in prompt.dialog_lines(FAKE_NAME, FAKE_PURPOSE, FAKE_GROUP, huge[0], huge, FAKE_CLIENT)
        if line.startswith("Argument list:")
    )
    assert len(line) <= len("Argument list: ") + prompt._ARGV_LIMIT


def test_dialog_timeout_text_follows_the_timeout_argument():
    text = "\n".join(
        prompt.dialog_lines(FAKE_NAME, FAKE_PURPOSE, FAKE_GROUP, FAKE_ARGV[0], FAKE_ARGV, FAKE_CLIENT, timeout_seconds=7.0)
    )
    assert "within 7 seconds" in text


def test_approver_from_dialog_passes_each_request_field_to_the_window(monkeypatch):
    calls: list[tuple] = []

    def fake_ask(*args):
        calls.append(args)
        return True

    monkeypatch.setattr(prompt, "ask_approval", fake_ask)
    request = ApprovalRequest(
        name=FAKE_NAME,
        purpose=FAKE_PURPOSE,
        group=FAKE_GROUP,
        command=FAKE_ARGV[0],
        argv=FAKE_ARGV,
        client=FAKE_CLIENT,
    )
    assert prompt.approver_from_dialog(request) is True
    assert calls == [(FAKE_NAME, FAKE_PURPOSE, FAKE_GROUP, FAKE_ARGV[0], FAKE_ARGV, FAKE_CLIENT)]


# -- window behaviour, with tkinter mocked ----------------------------------


@pytest.mark.parametrize(
    "script, expected",
    [
        (["approve"], True),
        (["deny"], False),
        (["close"], False),
        (["escape"], False),
        (["enter"], False),
        (["timeout"], False),
        ([], False),  # the loop ends with no answer
        (["deny", "approve"], False),  # the first answer wins
        (["approve", "deny"], True),
    ],
    ids=["approve", "deny", "close", "escape", "enter", "timeout", "no-answer", "deny-then-approve", "approve-then-deny"],
)
def test_only_a_click_on_approve_once_approves(script, expected):
    tk, _ = _tk_module(*script)
    assert _ask(tk) is expected


def test_the_window_is_destroyed_after_every_answer():
    tk, root = _tk_module("approve")
    _ask(tk)
    root.destroy.assert_called_once()


def test_the_default_timeout_is_120_seconds_and_denies():
    tk, root = _tk_module("timeout")
    assert _ask(tk) is False
    assert root.after.call_args.args[0] == 120_000


def test_a_custom_timeout_is_passed_to_the_window():
    tk, root = _tk_module()
    _ask(tk, timeout_seconds=2.5)
    assert root.after.call_args.args[0] == 2500


def test_the_window_has_a_title_is_topmost_and_is_not_resizable():
    tk, root = _tk_module("deny")
    _ask(tk)
    root.title.assert_called_once_with(prompt.WINDOW_TITLE)
    root.attributes.assert_called_once_with("-topmost", True)
    root.resizable.assert_called_once_with(False, False)


def test_the_buttons_are_labelled_approve_once_and_deny():
    tk, _ = _tk_module("deny")
    _ask(tk)
    labels = {call.kwargs["text"] for call in tk.Button.call_args_list}
    assert labels == {"Approve once", "Deny"}


def test_the_deny_button_has_focus_and_enter_and_escape_are_bound_to_deny():
    tk, root = _tk_module("deny")
    _ask(tk)
    buttons = tk.buttons_by_label
    buttons["Deny"].focus_set.assert_called_once()
    assert not buttons["Approve once"].focus_set.called
    bound_keys = {call.args[0] for call in root.bind.call_args_list}
    assert {"<Escape>", "<Return>"} <= bound_keys


def test_failing_to_open_the_window_denies_without_raising():
    tk = MagicMock(name="tkinter")
    tk.Tk.side_effect = RuntimeError("no display")
    assert _ask(tk) is False


def test_a_failure_inside_the_loop_denies_and_still_destroys_the_window():
    tk, root = _tk_module()
    root.mainloop.side_effect = RuntimeError("tk loop failed")
    assert _ask(tk) is False
    root.destroy.assert_called_once()


def test_missing_tkinter_denies(monkeypatch):
    monkeypatch.setitem(sys.modules, "tkinter", None)  # import raises ImportError
    assert prompt.ask_approval(FAKE_NAME, FAKE_PURPOSE, FAKE_GROUP, FAKE_ARGV[0], FAKE_ARGV, FAKE_CLIENT) is False
