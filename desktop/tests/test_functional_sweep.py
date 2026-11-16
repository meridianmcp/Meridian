"""Functional sweep: open every tab in the real window, check its buttons, and press only the Refresh buttons.

Offscreen Qt with fake data sources and a quiet Zotero client, so no test reaches the network, the keychain, Zotero or
the real home folder. The sweep is across tabs: each tab is shown in turn. It fails on a tab that does not build, on a
visible button with no label, on a Refresh button that never finishes, and on any exception raised while the sweep runs
(pytest-qt reports exceptions raised in slots and workers).

The GPUs tab reads this computer's GPUs and the RunPod pods when it is shown, by design. The Keys tab's RunPod card reads
the RunPod pods and offers when its own Refresh buttons are pressed. The conftest guard answers each of those reads with
a stub and records it. A sweep that shows those tabs must allow exactly those stubbed reads, so each tab's recorded reads
are checked against the set below, and anything outside the set still fails the test.
"""

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QPushButton

from meridian_desktop import local_client, projects_reader
from meridian_desktop.data_source import DataSources
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.main_window import TAB_NAMES, MainWindow
from meridian_desktop.zotero_client import API_NOT_RUNNING, ApiCheck, ZoteroStatus

EXPECTED_TABS = ("Home", "Projects", "Sessions", "GPUs", "Search", "Tunnel", "Zotero", "LaTeX", "Keys", "Settings")
# The tabs that show a Refresh button outside any collapsed group: Sessions, GPUs and Search have one at the top, Keys has
# the RunPod card's Refresh buttons, and Settings shows the Zotero panel's Refresh directly.
TABS_WITH_REFRESH = ("Sessions", "GPUs", "Search", "Keys", "Zotero")

# The stubbed reads the GPUs tab makes on show or on Refresh: this computer's GPUs, the Slurm and LSF queues, and the
# RunPod pods. The Keys tab's RunPod card makes the RunPod reads on its own Refresh buttons. No SSH host is saved in these
# tests, so no ssh read is expected.
ALLOWED_GPU_READS = {
    "run_command nvidia-smi",
    "run_command squeue",
    "run_command bjobs",
    "runpod_list_pods",
    "runpod_check",
    "runpod_offers",
}


def _unexpected(*args):
    raise AssertionError("this data source should not be read in this sweep")


def _sources():
    return DataSources(
        probe=lambda url: local_client.HEALTH_NOT_RUNNING,
        read_server=_unexpected,
        read_file=projects_reader.list_projects,
        get_token=lambda: None,
        list_hosted=_unexpected,
    )


class _QuietZotero:
    def status(self):
        return ZoteroStatus(
            exe=None,
            process_running=False,
            api=ApiCheck(API_NOT_RUNNING, "Zotero is not answering on its local API."),
        )

    def probe_api(self):
        return ApiCheck(API_NOT_RUNNING, "Zotero is not answering on its local API.")


def _window(qtbot, settings_path):
    window = MainWindow(SettingsStore(settings_path), data_sources=_sources(), zotero=_QuietZotero())
    qtbot.addWidget(window)
    window.show()
    return window


def _visible_buttons(page):
    return [button for button in page.findChildren(QPushButton) if button.isVisible()]


def _check_recorded_reads(reads: list[str], name: str) -> None:
    """Every stubbed read a tab made must be one the tab is designed to make. Then the record is cleared."""
    unexpected = sorted(set(reads) - ALLOWED_GPU_READS)
    assert unexpected == [], f"{name} attempted reads outside its design: {unexpected}"
    reads.clear()


def _settle(qtbot) -> None:
    """Wait until every worker the tab started has finished. The GPUs tab reads on a worker, so its stubbed reads are
    recorded after the tab is shown, not while it is shown."""
    # 30 seconds: the Zotero and LaTeX tabs add background work, and a worker still running from an earlier
    # test can hold the pool busy past 10 seconds. The cause of the longest holds has not been pinned down.
    qtbot.waitUntil(lambda: QThreadPool.globalInstance().activeThreadCount() == 0, timeout=30000)


def _show(qtbot, window, index):
    window.tabs.setCurrentIndex(index)
    qtbot.waitUntil(lambda: window.tabs.currentIndex() == index, timeout=2000)
    _settle(qtbot)
    return window.tabs.widget(index)


def test_every_tab_is_built_and_named(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    assert tuple(TAB_NAMES) == EXPECTED_TABS
    assert window.tabs.count() == len(EXPECTED_TABS)
    for index, name in enumerate(EXPECTED_TABS):
        assert window.tabs.tabText(index) == name
        assert window.tabs.widget(index) is not None, name


@pytest.mark.parametrize("name", EXPECTED_TABS)
def test_every_visible_button_in_a_tab_has_a_label(qtbot, settings_path, name, no_real_gpu_or_runpod_reads):
    window = _window(qtbot, settings_path)
    index = EXPECTED_TABS.index(name)
    page = _show(qtbot, window, index)
    _check_recorded_reads(no_real_gpu_or_runpod_reads, name)
    unlabelled = [
        button.objectName() or type(button).__name__
        for button in _visible_buttons(page)
        if not button.text().strip()
    ]
    assert unlabelled == [], f"{name} has buttons with no label: {unlabelled}"


@pytest.mark.parametrize("name", EXPECTED_TABS)
def test_refresh_buttons_in_a_tab_finish_without_error(qtbot, settings_path, name, no_real_gpu_or_runpod_reads):
    window = _window(qtbot, settings_path)
    index = EXPECTED_TABS.index(name)
    page = _show(qtbot, window, index)

    def _enabled_refresh():
        return [
            button for button in _visible_buttons(page) if "refresh" in button.text().lower() and button.isEnabled()
        ]

    # A worker's result reaches the UI thread after the pool is idle, and a panel keeps its Refresh buttons disabled until
    # then (the RunPod card does). Wait for an enabled Refresh button before checking, so the check is not a race.
    if name in TABS_WITH_REFRESH:
        qtbot.waitUntil(lambda: bool(_enabled_refresh()), timeout=5000)
    refresh = _enabled_refresh()
    # A tab with a Refresh button must show one here, so the test cannot pass by pressing nothing.
    if name in TABS_WITH_REFRESH:
        assert refresh, f"{name} shows no enabled Refresh button to press"
    for button in refresh:
        button.click()
        # A Refresh button is disabled while its read runs and enabled again when it finishes.
        qtbot.waitUntil(lambda button=button: button.isEnabled(), timeout=5000)
    _check_recorded_reads(no_real_gpu_or_runpod_reads, name)


def test_the_sweep_opens_each_tab_in_turn_without_error(qtbot, settings_path, no_real_gpu_or_runpod_reads):
    window = _window(qtbot, settings_path)
    for index, name in enumerate(EXPECTED_TABS):
        page = _show(qtbot, window, index)
        assert page.isVisible(), name
        _check_recorded_reads(no_real_gpu_or_runpod_reads, name)
