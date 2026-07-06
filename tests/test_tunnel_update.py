"""23ba76a2 — tunnel self-update policy: tiered modes + notify-then-confirm default."""
from __future__ import annotations

import pytest

from meridian.tunnel_client import _resolve_update_mode, _update_action

_OLD = "0.1.6"
_NEW = "0.2.0"


def test_resolve_update_mode_default_ask(monkeypatch):
    monkeypatch.delenv("MERIDIAN_TUNNEL_UPDATE_MODE", raising=False)
    assert _resolve_update_mode() == "ask"


@pytest.mark.parametrize("val,expected", [
    ("off", "off"), ("warn", "warn"), ("ask", "ask"), ("full-auto", "full-auto"),
    ("OFF", "off"), ("  Warn  ", "warn"),   # case- + whitespace-tolerant
    ("garbage", "ask"), ("", "ask"),         # unknown/blank -> safe default, never auto
])
def test_resolve_update_mode_parsing(monkeypatch, val, expected):
    monkeypatch.setenv("MERIDIAN_TUNNEL_UPDATE_MODE", val)
    assert _resolve_update_mode() == expected


def test_update_action_no_newer_version_is_none():
    # server not newer than local -> nothing, regardless of mode
    assert _update_action("ask", _NEW, _OLD, True) == "none"
    assert _update_action("full-auto", _OLD, _OLD, True) == "none"


def test_update_action_off_suppresses_even_when_newer():
    assert _update_action("off", _OLD, _NEW, True) == "none"


def test_update_action_warn_is_notify_only():
    assert _update_action("warn", _OLD, _NEW, True) == "notify"
    assert _update_action("warn", _OLD, _NEW, False) == "notify"


def test_update_action_ask_confirms_on_tty_notifies_otherwise():
    # the safe default: prompt only when we can actually read a keypress; a
    # backgrounded tunnel (no TTY) degrades to a notice, never a blocking prompt.
    assert _update_action("ask", _OLD, _NEW, True) == "confirm"
    assert _update_action("ask", _OLD, _NEW, False) == "notify"


def test_update_action_full_auto_updates():
    assert _update_action("full-auto", _OLD, _NEW, True) == "auto"


def test_update_action_unknown_mode_defaults_to_ask():
    assert _update_action("bogus", _OLD, _NEW, True) == "confirm"
    assert _update_action("bogus", _OLD, _NEW, False) == "notify"
