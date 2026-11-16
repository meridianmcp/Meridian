"""The GPUs tab's offer filters are saved when the owner changes them and put back when the tab is built again.

A saved maker or data center waits until a check lists it. A saved value with the wrong type is dropped with a note. No
network: the panel gets the fake catalog from test_gpu_offers.
"""

import json

from PySide6.QtWidgets import QCheckBox, QComboBox, QDoubleSpinBox, QLabel, QLineEdit, QPushButton, QSpinBox

from meridian_desktop.gpu_catalog import NO_KEY_MESSAGE, TIER_SECURE, GpuCatalogError
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.gpu_offers_panel import CHECKING_TEXT, SAVE_DELAY_MS, GpuOffersPanel

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
    panel.flush_saved_filters()
    assert saved[-1]["memory_gb"] == 40
    assert saved[-1]["in_stock"] is True


def test_the_saved_filters_are_put_back_when_the_tab_is_built_again(qtbot):
    filters = {
        "memory_gb": 40,
        "tier": TIER_SECURE,
        "in_stock": False,
        "sort": "tflops_desc",
        "max_price": 1.5,
    }
    panel = _panel(qtbot, saved_filters=filters)
    assert panel.findChild(QSpinBox, "gpu-offers-memory").value() == 40
    assert panel.findChild(QComboBox, "gpu-offers-tier").currentData() == TIER_SECURE
    assert panel.findChild(QCheckBox, "gpu-offers-in-stock").isChecked() is False
    assert panel.findChild(QComboBox, "gpu-offers-sort").currentData() == "tflops_desc"
    assert panel.findChild(QDoubleSpinBox, "gpu-offers-price").value() == 1.5


def test_restoring_the_filters_does_not_write_them_back(qtbot):
    saved = []
    _panel(qtbot, saved_filters={"memory_gb": 24}, on_filters_changed=lambda f: saved.append(f) or True)
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
    panel.flush_saved_filters()
    panel.findChild(QSpinBox, "gpu-offers-memory").setValue(24)
    panel.flush_saved_filters()
    assert len(attempts) == 2
    assert attempts[-1]["memory_gb"] == 24


def test_the_filters_round_trip_through_the_settings_file(tmp_path):
    path = tmp_path / "settings.json"
    SettingsStore(path).set_gpu_offer_filters({"memory_gb": 24, "max_price": 0.75, "in_stock": False})
    loaded = SettingsStore(path).load()
    assert loaded.settings.gpu_offer_filters == {"memory_gb": 24, "max_price": 0.75, "in_stock": False}
    assert loaded.warning is None


def test_no_saved_filters_is_the_default_and_writes_no_key(tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    assert store.load().settings.gpu_offer_filters == {}
    store.set_gpu_offer_filters({"memory_gb": 24})
    store.set_gpu_offer_filters({})
    assert "gpu_offer_filters" not in json.loads(path.read_text(encoding="utf-8"))


def test_a_saved_filter_with_the_wrong_type_is_dropped_and_noted(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"gpu_offer_filters": {"memory_gb": "forty", "tier": "secure", "unknown": 1, "in_stock": True}}),
        encoding="utf-8",
    )
    loaded = SettingsStore(path).load()
    assert loaded.settings.gpu_offer_filters == {"tier": "secure", "in_stock": True}
    assert loaded.warning is not None
    assert "memory_gb" in loaded.warning


# ----------------------------------------------------------- review fixes


def _wait_for_check(qtbot, panel):
    check = panel.findChild(QPushButton, "gpu-offers-check")
    status = panel.findChild(QLabel, "gpu-offers-status")
    qtbot.waitUntil(lambda: check.isEnabled() and status.text() != CHECKING_TEXT, timeout=5000)


def _check_again(qtbot, panel):
    panel.findChild(QPushButton, "gpu-offers-check").click()
    _wait_for_check(qtbot, panel)


def test_the_owner_pick_replaces_a_saved_maker_that_is_not_listed_and_survives_a_recheck(qtbot):
    saved = []
    panel = _panel(qtbot, saved_filters={"maker": "Intel"}, on_filters_changed=lambda f: saved.append(f) or True)
    panel.show()
    qtbot.waitExposed(panel)
    _wait_for_check(qtbot, panel)
    maker = panel.findChild(QComboBox, "gpu-offers-maker")
    maker.setCurrentIndex(maker.findData("AMD"))
    panel.flush_saved_filters()
    assert saved[-1]["maker"] == "AMD"
    _check_again(qtbot, panel)
    assert maker.currentData() == "AMD"
    panel.flush_saved_filters()
    assert saved[-1]["maker"] == "AMD"


def test_the_owner_pick_replaces_a_saved_data_center_that_is_not_listed(qtbot):
    saved = []
    panel = _panel(qtbot, saved_filters={"center": "EU-XX-1"}, on_filters_changed=lambda f: saved.append(f) or True)
    panel.show()
    qtbot.waitExposed(panel)
    _wait_for_check(qtbot, panel)
    center = panel.findChild(QComboBox, "gpu-offers-center")
    center.setCurrentIndex(center.findData("US-KS-2"))
    _check_again(qtbot, panel)
    assert center.currentData() == "US-KS-2"
    panel.flush_saved_filters()
    assert saved[-1]["center"] == "US-KS-2"


def test_a_check_that_lists_no_gpu_types_says_so(qtbot):
    panel = GpuOffersPanel(key_reader=lambda: "KEY", fetch=_FakeCatalog(offers=()))
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitExposed(panel)
    _wait_for_check(qtbot, panel)
    assert panel.findChild(QLabel, "gpu-offers-status").text().startswith("RunPod lists no GPU types right now.")


def test_a_failed_recheck_empties_the_choices_and_keeps_a_pending_value_pending(qtbot):
    catalog = _FakeCatalog()
    saved = []
    panel = GpuOffersPanel(
        key_reader=lambda: "KEY",
        fetch=catalog,
        saved_filters={"maker": "Intel"},
        on_filters_changed=lambda f: saved.append(f) or True,
    )
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitExposed(panel)
    _wait_for_check(qtbot, panel)
    center = panel.findChild(QComboBox, "gpu-offers-center")
    maker = panel.findChild(QComboBox, "gpu-offers-maker")
    assert center.count() > 1 and maker.count() > 1
    catalog.failure = GpuCatalogError(NO_KEY_MESSAGE)
    _check_again(qtbot, panel)
    assert center.count() == 1 and center.currentData() == ""
    assert maker.count() == 1 and maker.currentData() == ""
    assert panel.findChild(QLabel, "gpu-offers-status").text() == NO_KEY_MESSAGE
    # The saved maker still waits for a check that lists it, so it is not saved away as "any".
    panel.findChild(QSpinBox, "gpu-offers-memory").setValue(8)
    panel.flush_saved_filters()
    assert saved[-1]["maker"] == "Intel"


def test_several_quick_changes_are_saved_in_one_write_after_the_delay(qtbot):
    saved = []
    panel = _panel(qtbot, on_filters_changed=lambda f: saved.append(f) or True)
    memory = panel.findChild(QSpinBox, "gpu-offers-memory")
    for value in (8, 16, 24, 32):
        memory.setValue(value)
    assert saved == []  # nothing is written while the owner is still changing it
    qtbot.waitUntil(lambda: len(saved) == 1, timeout=SAVE_DELAY_MS * 4)
    qtbot.wait(SAVE_DELAY_MS + 100)
    assert len(saved) == 1
    assert saved[0]["memory_gb"] == 32


def test_a_waiting_change_is_saved_when_the_panel_is_hidden(qtbot):
    saved = []
    panel = _panel(qtbot, on_filters_changed=lambda f: saved.append(f) or True)
    panel.show()
    qtbot.waitExposed(panel)
    panel.findChild(QSpinBox, "gpu-offers-memory").setValue(48)
    panel.hide()
    assert saved and saved[-1]["memory_gb"] == 48


def test_the_name_box_filters_but_is_never_saved_or_restored(qtbot, tmp_path):
    saved = []
    panel = _panel(qtbot, saved_filters={"name": "a100"}, on_filters_changed=lambda f: saved.append(f) or True)
    name = panel.findChild(QLineEdit, "gpu-offers-name")
    assert name.text() == ""  # a name an older build saved is not put back
    name.setText("rpa_not_a_real_key")
    panel.findChild(QSpinBox, "gpu-offers-memory").setValue(8)
    panel.flush_saved_filters()
    assert saved and all("name" not in state for state in saved)
    path = tmp_path / "settings.json"
    SettingsStore(path).set_gpu_offer_filters({"name": "rpa_not_a_real_key", "memory_gb": 8})
    assert "rpa_not_a_real_key" not in path.read_text(encoding="utf-8")


def test_the_gpu_setters_keep_every_known_setting_and_drop_an_unknown_key(tmp_path):
    # save() rebuilds the file from the settings this build knows, so an unknown key (a token-like one included) is
    # dropped, as test_settings_store pins. Each setter keeps every setting the others wrote.
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"from_a_newer_build": {"keep": [1, 2]}}), encoding="utf-8")
    store = SettingsStore(path)
    store.set_gpu_refresh_seconds(60)
    store.set_gpu_offer_filters({"memory_gb": 24})
    store.set_sessions_only_active(True)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "from_a_newer_build" not in data
    assert data["gpu_refresh_seconds"] == 60
    assert data["gpu_offer_filters"] == {"memory_gb": 24}
    assert data["sessions_only_active_projects"] is True
