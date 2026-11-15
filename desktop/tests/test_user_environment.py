"""Tests for the Windows user variable writer (meridian_desktop/user_environment.py). Pure Python, no Qt.

Every write goes to FakeUserRegistry, so nothing touches HKCU. conftest also makes the real registry refuse, so a test that
forgot to inject a fake fails loudly instead of writing. FAKE_VALUE is an obviously fake value, so a leak check can search for it.
"""

from __future__ import annotations

import pytest

from _fakes import FakeUserRegistry
from meridian_desktop import user_environment
from meridian_desktop.user_environment import (
    ENVIRONMENT_SUBKEY,
    RESERVED_NAMES,
    UserEnvironment,
    UserEnvironmentError,
    variable_name_problem,
)

FAKE_VALUE = "sk-fake-user-env-0001"


class _ExplodingRegistry:
    """Every call fails, and each failure message carries the value on purpose."""

    def __init__(self, secret: str) -> None:
        self._secret = secret

    def write_user_value(self, name: str, value: str) -> None:
        raise PermissionError(f"write refused for {value}")

    def announce_change(self) -> bool:
        raise OSError(f"broadcast failed while holding {self._secret}")


class _ExplodingAnnounce(FakeUserRegistry):
    def announce_change(self) -> bool:
        raise OSError(f"broadcast failed while holding {FAKE_VALUE}")


# ----------------------------------------------------------- what is written


def test_the_value_is_written_under_the_variable_name_and_the_change_is_announced():
    registry = FakeUserRegistry(announced=True)

    announced = UserEnvironment(registry).set("OPENAI", FAKE_VALUE)

    assert registry.writes == [("OPENAI", FAKE_VALUE)]
    assert registry.announcements == 1
    assert announced is True


def test_an_unconfirmed_broadcast_is_reported_and_the_value_stays_written():
    registry = FakeUserRegistry(announced=False)

    announced = UserEnvironment(registry).set("OPENAI", FAKE_VALUE)

    assert announced is False
    assert registry.writes == [("OPENAI", FAKE_VALUE)], "the write happens before the broadcast, and is not undone"


def test_a_broadcast_that_raises_is_reported_as_unconfirmed_and_the_value_stays_written():
    registry = _ExplodingAnnounce()

    announced = UserEnvironment(registry).set("OPENAI", FAKE_VALUE)

    assert announced is False
    assert registry.writes == [("OPENAI", FAKE_VALUE)]


def test_the_registry_key_and_broadcast_constants_are_the_user_environment():
    assert ENVIRONMENT_SUBKEY == "Environment", "the user variables live under HKCU\\Environment"
    assert user_environment.WM_SETTINGCHANGE == 0x001A
    assert user_environment.HWND_BROADCAST == 0xFFFF


# ----------------------------------------------------------- refused before any write


@pytest.mark.parametrize("name", ["PATH", "path", "Path", "Temp", "TMP", "USERPROFILE", "PYTHONPATH", "SystemRoot"])
def test_a_reserved_windows_variable_is_refused_before_anything_is_written(name):
    registry = FakeUserRegistry()

    with pytest.raises(UserEnvironmentError):
        UserEnvironment(registry).set(name, FAKE_VALUE)

    assert registry.writes == [] and registry.announcements == 0


@pytest.mark.parametrize("name", ["2FA", "A-B", "A B", "", "A.B", "=X", "X\x00"])
def test_a_name_that_is_not_a_plain_variable_name_is_refused(name):
    registry = FakeUserRegistry()

    with pytest.raises(UserEnvironmentError):
        UserEnvironment(registry).set(name, FAKE_VALUE)

    assert registry.writes == []


@pytest.mark.parametrize("value", ["", "   ", None, 12])
def test_an_empty_or_non_text_value_is_refused(value):
    registry = FakeUserRegistry()

    with pytest.raises(UserEnvironmentError):
        UserEnvironment(registry).set("OPENAI", value)

    assert registry.writes == []


def test_a_value_with_a_nul_character_is_refused_and_the_value_is_not_shown():
    registry = FakeUserRegistry()
    value = FAKE_VALUE + "\x00tail"

    with pytest.raises(UserEnvironmentError) as caught:
        UserEnvironment(registry).set("OPENAI", value)

    assert registry.writes == []
    assert FAKE_VALUE not in str(caught.value)


def test_variable_name_problem_accepts_plain_names_and_refuses_the_rest():
    assert variable_name_problem("OPENAI") is None
    assert variable_name_problem("RUNPOD_API_KEY") is None
    assert variable_name_problem("PATH") is not None
    assert variable_name_problem("2FA") is not None
    assert "PATH" in RESERVED_NAMES


# ----------------------------------------------------------- failures never carry the value


def test_a_failed_write_names_the_step_and_the_class_only():
    with pytest.raises(UserEnvironmentError) as caught:
        UserEnvironment(_ExplodingRegistry(FAKE_VALUE)).set("OPENAI", FAKE_VALUE)

    text = str(caught.value)
    assert "write" in text and "PermissionError" in text
    assert FAKE_VALUE not in text


def test_a_failed_write_drops_the_original_exception_so_no_traceback_carries_the_value():
    with pytest.raises(UserEnvironmentError) as caught:
        UserEnvironment(_ExplodingRegistry(FAKE_VALUE)).set("OPENAI", FAKE_VALUE)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert caught.value.__suppress_context__ is True


def test_a_failed_write_is_never_followed_by_a_broadcast():
    registry = FakeUserRegistry(fail_write=PermissionError("denied"))

    with pytest.raises(UserEnvironmentError):
        UserEnvironment(registry).set("OPENAI", FAKE_VALUE)

    assert registry.announcements == 0, "a change that did not land is not announced"
