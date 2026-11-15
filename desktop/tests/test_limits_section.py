"""Tests for the Limits block on the Settings tab (meridian_desktop/ui/limits_section.py), with pytest-qt.

The block holds one typed download cap with an MB, GB or TB unit. Offscreen Qt (conftest). Every save goes to a settings
file in pytest's temp dir, so nothing touches the real one.
"""

from __future__ import annotations

import json

import pytest
from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QLabel

from meridian_desktop.settings_store import LIMIT_SPECS, SettingsStore
from meridian_desktop.ui.limits_section import (
    LIMIT_ROWS,
    LIMITS_INTRO,
    NOT_SAVED_TEXT,
    LimitsSection,
    from_megabytes,
    to_megabytes,
)

KEY = "limit_zip_download_mb"
SPEC = {spec.key: spec for spec in LIMIT_SPECS}[KEY]


def _saved(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _amount(section: LimitsSection) -> QDoubleSpinBox:
    found = section.findChild(QDoubleSpinBox, f"limit-amount-{KEY}")
    assert found is not None
    return found


def _unit(section: LimitsSection) -> QComboBox:
    return section.findChild(QComboBox, f"limit-unit-{KEY}")


def _range(section: LimitsSection) -> str:
    return section.findChild(QLabel, f"limit-range-{KEY}").text()


def _status(section: LimitsSection) -> QLabel:
    return section.findChild(QLabel, "limits-status")


def _enter(section: LimitsSection) -> None:
    """What pressing Enter in the box does: the box reports editingFinished, and the block saves the typed value."""
    _amount(section).editingFinished.emit()


@pytest.fixture
def section(qtbot, settings_path):
    widget = LimitsSection(SettingsStore(settings_path))
    qtbot.addWidget(widget)
    return widget


def test_the_block_holds_one_typed_download_cap_with_mb_gb_and_tb(section):
    assert [row.key for row in LIMIT_ROWS] == [KEY]
    assert [_unit(section).itemText(index) for index in range(_unit(section).count())] == ["MB", "GB", "TB"]


def test_the_default_is_one_thousand_megabytes_shown_in_mb(section):
    assert _unit(section).currentText() == "MB"
    assert _amount(section).value() == 1000
    assert _range(section) == "10 to 10,000 MB"


def test_the_intro_says_it_is_saved_and_not_enforced_yet(section):
    assert section.findChild(QLabel, "limits-intro").text() == LIMITS_INTRO
    assert "not enforced yet" in LIMITS_INTRO


def test_showing_the_saved_value_saves_nothing(section, settings_path):
    assert _saved(settings_path) == {}


def test_a_typed_number_is_saved_when_enter_is_pressed(section, settings_path):
    _amount(section).setValue(250)
    _enter(section)

    assert _saved(settings_path)[KEY] == 250
    assert _amount(section).value() == 250


def test_the_unit_shows_the_same_saved_value_in_gigabytes_and_saves_nothing(section, settings_path):
    _amount(section).setValue(2048)
    _enter(section)
    before = settings_path.read_text(encoding="utf-8")

    _unit(section).setCurrentIndex(1)  # GB

    assert _amount(section).value() == 2
    assert _range(section) == "0.01 to 9.77 GB"
    assert settings_path.read_text(encoding="utf-8") == before


def test_a_number_typed_in_gigabytes_is_saved_as_megabytes(section, settings_path):
    _unit(section).setCurrentIndex(1)  # GB
    _amount(section).setValue(1.5)
    _enter(section)

    assert _saved(settings_path)[KEY] == 1536
    assert _amount(section).value() == 1.5


def test_a_number_typed_in_terabytes_is_saved_as_megabytes(section, settings_path):
    _unit(section).setCurrentIndex(2)  # TB; the range tops out at 10,000 MB, about 0.01 TB
    _amount(section).setValue(0.005)
    _enter(section)

    assert _saved(settings_path)[KEY] == round(0.005 * 1024 * 1024)


def test_an_amount_beyond_the_range_is_clamped_by_the_store_and_shown_as_kept(section, settings_path):
    _amount(section).setValue(999999)
    _enter(section)

    assert _saved(settings_path)[KEY] == SPEC.maximum
    assert _amount(section).value() == SPEC.maximum


def test_a_refused_save_says_so_and_the_box_shows_the_saved_value(qtbot, settings_path, monkeypatch):
    section = LimitsSection(SettingsStore(settings_path))
    qtbot.addWidget(section)

    def refuse(key, value):
        raise OSError("disk full")

    monkeypatch.setattr(section._store, "set_limit", refuse)
    _amount(section).setValue(300)
    _enter(section)

    assert _status(section).isHidden() is False
    assert _status(section).text() == NOT_SAVED_TEXT.format(name="OSError")
    assert _amount(section).value() == 1000


def test_an_unreadable_settings_file_is_not_overwritten(qtbot, settings_path):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("{not json", encoding="utf-8")
    before = settings_path.read_text(encoding="utf-8")
    section = LimitsSection(SettingsStore(settings_path))
    qtbot.addWidget(section)

    _amount(section).setValue(300)
    _enter(section)

    assert settings_path.read_text(encoding="utf-8") == before
    assert _status(section).isHidden() is False
    assert _status(section).text() != ""


def test_megabyte_conversions_round_to_whole_megabytes():
    assert to_megabytes(1.5, 1024) == 1536
    assert to_megabytes(0.001, 1) == 0
    assert from_megabytes(1536, 1024) == 1.5
