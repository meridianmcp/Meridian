"""The GPUs tab's offer filters are saved when the owner changes them and put back when the tab is built again.

A saved maker or data center waits until a check lists it. A saved value with the wrong type is dropped with a note. No
network: the panel gets the fake catalog from test_gpu_offers.
"""

import json

from PySide6.QtWidgets import QCheckBox, QComboBox, QDoubleSpinBox, QLabel, QLineEdit, QPushButton, QSpinBox

from meridian_desktop.gpu_catalog import TIER_SECURE
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.gpu_offers_panel import CHECKING_TEXT, GpuOffersPanel

from test_gpu_offers import _FakeCatalog


def _panel(qtbot, **kwargs):
    panel = GpuOffersPanel(key_reader=lambda: "KEY", fetch=_FakeCatalog(), **kwargs)
    qtbot.addWidget(panel)
    return panel


def test_a_change_is_passed_to_the_save_callback_with_the_filters_as_shown(qtbot):
    saved = []

    def record(filters):
        saved.append(filters)
        return True

    panel = _panel(qtbot, on_filters_changed=record)
    assert saved == []  # building the tab with nothing saved changes nothing, so nothing is written
    panel.findChild(QSpinBox, "gpu-offers-memory").setValue(40)
    assert saved[-1]["memory_gb"] == 40
    assert saved[-1]["in_stock"] is True


def test_the_saved_filters_are_put_back_when_the_tab_is_built_again(qtbot):
    filters = {
        "name": "a100",
        "memory_gb": 40,
        "tier": TIER_SECURE,
        "in_stock": False,
        "sort": "tflops_desc",
        "max_price": 1.5,
    }
    panel = _panel(qtbot, saved_filters=filters)
    assert panel.findChild(QLineEdit, "gpu-offers-name").text() == "a100"
    assert panel.findChild(QSpinBox, "gpu-offers-memory").value() == 40
    assert panel.findChild(QComboBox, "gpu-offers-tier").currentData() == TIER_SECURE
    assert panel.findChild(QCheckBox, "gpu-offers-in-stock").isChecked() is False
    assert panel.findChild(QComboBox, "gpu-offers-sort").currentData() == "tflops_desc"
    assert panel.findChild(QDoubleSpinBox, "gpu-offers-price").value() == 1.5


def test_restoring_the_filters_does_not_write_them_back(qtbot):
    saved = []
    _panel(qtbot, saved_filters={"name": "l4", "memory_gb": 24}, on_filters_changed=lambda f: saved.append(f) or True)
    assert saved == []


def test_a_saved_maker_is_chosen_once_a_check_lists_it(qtbot):
    panel = _panel(qtbot, saved_filters={"maker": "AMD"})
    panel.show()
    qtbot.waitExposed(panel)
    check = panel.findChild(QPushButton, "gpu-offers-check")
    status = panel.findChild(QLabel, "gpu-offers-status")
    qtbot.waitUntil(lambda: check.isEnabled() and status.text() != CHECKING_TEXT, timeout=5000)
    assert panel.findChild(QComboBox, "gpu-offers-maker").currentData() == "AMD"


def test_a_failed_write_is_tried_again_on_the_next_change(qtbot):
    attempts = []

    def fail_then_work(filters):
        attempts.append(filters)
        return len(attempts) > 1

    panel = _panel(qtbot, on_filters_changed=fail_then_work)
    panel.findChild(QSpinBox, "gpu-offers-memory").setValue(16)
    panel.findChild(QSpinBox, "gpu-offers-memory").setValue(24)
    assert len(attempts) == 2
    assert attempts[-1]["memory_gb"] == 24


def test_the_filters_round_trip_through_the_settings_file(tmp_path):
    path = tmp_path / "settings.json"
    SettingsStore(path).set_gpu_offer_filters({"name": "l4", "memory_gb": 24, "max_price": 0.75, "in_stock": False})
    loaded = SettingsStore(path).load()
    assert loaded.settings.gpu_offer_filters == {"name": "l4", "memory_gb": 24, "max_price": 0.75, "in_stock": False}
    assert loaded.warning is None


def test_no_saved_filters_is_the_default_and_writes_no_key(tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    assert store.load().settings.gpu_offer_filters == {}
    store.set_gpu_offer_filters({"name": "l4"})
    store.set_gpu_offer_filters({})
    assert "gpu_offer_filters" not in json.loads(path.read_text(encoding="utf-8"))


def test_a_saved_filter_with_the_wrong_type_is_dropped_and_noted(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"gpu_offer_filters": {"memory_gb": "forty", "name": "l4", "unknown": 1, "in_stock": True}}),
        encoding="utf-8",
    )
    loaded = SettingsStore(path).load()
    assert loaded.settings.gpu_offer_filters == {"name": "l4", "in_stock": True}
    assert loaded.warning is not None
    assert "memory_gb" in loaded.warning
