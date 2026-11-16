"""Tests for the RunPod panel (meridian_desktop/ui/runpod_panel.py), with pytest-qt.

The store and the local client are fakes, so nothing touches the keychain or the network.
The settings file is a temp file. Offscreen Qt, from conftest.
"""

import copy
import gc
import threading
import weakref

import pytest
import shiboken6
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import QCheckBox, QLabel, QLineEdit, QPushButton, QTableWidget, QWidget

from meridian_desktop.local_client import (
    HEALTH_ERROR,
    HEALTH_NOT_MERIDIAN,
    HEALTH_NOT_RUNNING,
    HEALTH_RUNNING,
    LocalClientError,
    offers_path,
)
from meridian_desktop.secret_store import SecretStoreError
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui.runpod_panel import (
    KEY_MISSING_TEXT,
    KEY_STORED_TEXT,
    POD_COLUMNS,
    REMOVE_KEY_BODY,
    REMOVE_KEY_CANCELLED_TEXT,
    REMOVE_KEY_FAILED_TEXT,
    OFFER_CAP_INVALID_TEXT,
    OFFERS_NOT_CONFIGURED_TEXT,
    REMOVE_KEY_TITLE,
    RunPodPanel,
    describe_offers,
    offer_error_code,
    offer_query,
    offer_row,
    offer_sort_value,
)

from _shared_delivery import emit, recording_deliveries, visible_state

FAKE_KEY = "test-key-not-real"
LOCAL_URL = "http://127.0.0.1:7878"

POD_A = {
    "id": "pod-a1",
    "name": "trainer",
    "status": "RUNNING",
    "gpu": {"id": "NVIDIA RTX A4000", "count": 1},
    "costPerHour": 0.34,
    "uptimeSeconds": 7200,
}
POD_B = {
    "id": "pod-b2",
    "name": "idle-box",
    "status": "EXITED",
    "gpu": None,
    "costPerHour": 0.0,
    "uptimeSeconds": None,
}


class FakeStore:
    """An in-memory secret store. It records each call and can be told to fail."""

    def __init__(self, value=None):
        self.value = value
        self.failure = None
        self.calls = []

    def _enter(self, operation):
        self.calls.append(operation)
        if self.failure is not None:
            raise self.failure

    def get_secret(self, service, account):
        self._enter("get")
        return self.value

    def set_secret(self, service, account, value):
        self._enter("set")
        cleaned = value.strip()
        if not cleaned:
            raise SecretStoreError("secret is empty")
        self.value = cleaned

    def delete_secret(self, service, account):
        self._enter("delete")
        if self.value is None:
            return False
        self.value = None
        return True


class FakeClient:
    """A local client that returns canned answers and records each call and its thread."""

    def __init__(self):
        self.state = HEALTH_RUNNING
        self.check = {
            "ok": True,
            "credential_source": "keyring",
            "pod_count": 2,
            "billing_available": True,
            "error": None,
        }
        self.listing = {
            "ok": True,
            "credential_source": "keyring",
            "pods": [POD_A, POD_B],
            "pod_count": 2,
            "has_more_pods": False,
            "error": None,
        }
        self.offers = {
            "ok": True,
            "status": "ok",
            "count": 0,
            "offers": [],
            "providers": [],
            "error": None,
        }
        self.offer_queries = []
        self.raise_on = {}
        self.gate = None
        self.calls = []
        self.threads = []

    def _note(self, name, base_url, timeout):
        self.calls.append((name, base_url, timeout))
        self.threads.append(threading.current_thread())
        if name in self.raise_on:
            raise self.raise_on[name]

    def probe_health(self, base_url, timeout):
        self._note("probe_health", base_url, timeout)
        if self.gate is not None:
            self.gate.wait(5)
        return self.state

    def runpod_check(self, base_url, timeout):
        self._note("runpod_check", base_url, timeout)
        return copy.deepcopy(self.check)

    def runpod_list_pods(self, base_url, timeout):
        self._note("runpod_list_pods", base_url, timeout)
        return copy.deepcopy(self.listing)

    def runpod_offers(self, base_url, query, timeout):
        self._note("runpod_offers", base_url, timeout)
        self.offer_queries.append(dict(query or {}))
        return copy.deepcopy(self.offers)


class FakeConfirm:
    """The Yes or No stand-in. It records each question and returns the answer it was given."""

    def __init__(self, answer=True):
        self.answer = answer
        self.calls = []

    def __call__(self, parent, title, text):
        self.calls.append((title, text))
        return self.answer


def wait_idle(qtbot, panel):
    qtbot.waitUntil(lambda: not panel.busy, timeout=5000)


def press(qtbot, panel, name):
    """Click a named button, then wait for its job to finish."""
    panel.findChild(QPushButton, name).click()
    wait_idle(qtbot, panel)


def text_of(panel, name):
    return panel.findChild(QLabel, name).text()


def all_text(panel):
    """Every string a user can read in the panel: labels, buttons, fields, and table cells."""
    chunks = []
    for widget in [panel, *panel.findChildren(QWidget)]:
        for getter in ("text", "placeholderText", "windowTitle", "toolTip"):
            method = getattr(widget, getter, None)
            if callable(method):
                try:
                    value = method()
                except TypeError:
                    continue
                if isinstance(value, str):
                    chunks.append(value)
    table = panel.findChild(QTableWidget)
    if table is not None:
        for column in range(table.columnCount()):
            header = table.horizontalHeaderItem(column)
            if header is not None:
                chunks.append(header.text())
        for row in range(table.rowCount()):
            for column in range(table.columnCount()):
                item = table.item(row, column)
                if item is not None:
                    chunks.append(item.text())
    return "\n".join(chunks)


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def make_panel(qtbot, settings_path):
    def build(store, client, confirm=None):
        SettingsStore(settings_path).save(Settings(local_url=LOCAL_URL))
        # Every panel in these tests gets a Yes-answering stand-in unless a test passes its own, so no dialog can open.
        answer = FakeConfirm() if confirm is None else confirm
        panel = RunPodPanel(SettingsStore(settings_path), store, client, confirm=answer)
        qtbot.addWidget(panel)
        wait_idle(qtbot, panel)
        return panel

    return build


# ----------------------------------------------------------------- key status


def test_status_shows_missing_when_no_key_is_stored(make_panel, store, client):
    panel = make_panel(store, client)
    assert text_of(panel, "runpod-key-status") == KEY_MISSING_TEXT


def test_status_shows_stored_without_showing_the_value(make_panel, client):
    panel = make_panel(FakeStore(FAKE_KEY), client)
    assert text_of(panel, "runpod-key-status") == KEY_STORED_TEXT
    assert FAKE_KEY not in all_text(panel)


def test_status_reports_a_failed_read_in_plain_words(make_panel, client):
    store = FakeStore()
    store.failure = SecretStoreError("keyring read failed (RuntimeError)")
    panel = make_panel(store, client)
    assert text_of(panel, "runpod-key-status") == (
        "Key storage could not be read. keyring read failed (RuntimeError)"
    )


# ----------------------------------------------------------------- save


def test_save_stores_the_key_clears_the_field_and_hides_the_value(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    field = panel.findChild(QLineEdit, "runpod-key-input")
    field.setText(FAKE_KEY)

    press(qtbot, panel, "runpod-save-key")

    assert store.value == FAKE_KEY
    assert field.text() == ""
    assert text_of(panel, "runpod-key-status") == KEY_STORED_TEXT
    assert text_of(panel, "runpod-result") == "Key saved in the keychain."
    assert FAKE_KEY not in all_text(panel)


def test_save_strips_surrounding_whitespace(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    panel.findChild(QLineEdit, "runpod-key-input").setText(
        f"  {FAKE_KEY}  "
    )
    press(qtbot, panel, "runpod-save-key")
    assert store.value == FAKE_KEY


def test_save_with_blank_field_makes_no_store_call(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    panel.findChild(QLineEdit, "runpod-key-input").setText("   ")
    press(qtbot, panel, "runpod-save-key")
    assert store.calls == ["get"]  # only the start-up status read
    assert text_of(panel, "runpod-result") == "Type a key first, then press Save key."


def test_save_failure_shows_the_store_message_and_keeps_the_field(qtbot, make_panel, client):
    store = FakeStore()
    panel = make_panel(store, client)
    field = panel.findChild(QLineEdit, "runpod-key-input")
    field.setText(FAKE_KEY)
    store.failure = SecretStoreError("keyring save failed (ValueError)")

    press(qtbot, panel, "runpod-save-key")

    assert text_of(panel, "runpod-result") == "keyring save failed (ValueError)"
    assert store.value is None
    assert field.text() == FAKE_KEY


# ----------------------------------------------------------------- remove


def test_remove_asks_first_and_removes_the_key_on_yes(qtbot, make_panel, client):
    store = FakeStore(FAKE_KEY)
    confirm = FakeConfirm(answer=True)
    panel = make_panel(store, client, confirm)
    press(qtbot, panel, "runpod-remove-key")
    assert confirm.calls == [(REMOVE_KEY_TITLE, REMOVE_KEY_BODY)], "the owner is asked once, before anything is deleted"
    assert store.value is None
    assert text_of(panel, "runpod-key-status") == KEY_MISSING_TEXT
    assert text_of(panel, "runpod-result") == "Key removed from the keychain."


def test_remove_on_no_changes_nothing_and_makes_no_delete_call(qtbot, make_panel, client):
    store = FakeStore(FAKE_KEY)
    confirm = FakeConfirm(answer=False)
    panel = make_panel(store, client, confirm)
    press(qtbot, panel, "runpod-remove-key")
    assert len(confirm.calls) == 1
    assert "delete" not in store.calls, "the keychain is not touched after a No"
    assert store.value == FAKE_KEY
    assert text_of(panel, "runpod-key-status") == KEY_STORED_TEXT
    assert text_of(panel, "runpod-result") == REMOVE_KEY_CANCELLED_TEXT
    assert FAKE_KEY not in all_text(panel)


def test_remove_failure_from_the_disk_shows_one_plain_line_and_keeps_the_key(qtbot, make_panel, client):
    store = FakeStore(FAKE_KEY)
    panel = make_panel(store, client)
    store.failure = OSError("C:\\Users\\example\\keyring\\secret.db is locked")
    press(qtbot, panel, "runpod-remove-key")
    assert text_of(panel, "runpod-result") == REMOVE_KEY_FAILED_TEXT
    assert store.value == FAKE_KEY
    assert "C:\\Users" not in all_text(panel), "the system text of the error never reaches the panel"


def test_remove_reports_when_nothing_was_stored(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-remove-key")
    assert text_of(panel, "runpod-result") == "No key was stored, so nothing was removed."


def test_an_empty_pod_list_shows_one_plain_line_and_no_empty_grid(qtbot, make_panel, store, client):
    client.listing = {**client.listing, "pods": [], "pod_count": 0}
    panel = make_panel(store, client)
    grid = panel.findChild(QTableWidget, "runpod-pods")
    assert grid.isHidden() and not panel.findChild(QLabel, "runpod-pods-empty").isHidden(), "before any listing"
    press(qtbot, panel, "runpod-refresh-pods")
    assert grid.isHidden()
    assert text_of(panel, "runpod-pods-empty") == "Nothing here yet."


def test_a_listing_with_pods_shows_the_grid_and_hides_the_plain_line(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-refresh-pods")
    assert not panel.findChild(QTableWidget, "runpod-pods").isHidden()
    assert panel.findChild(QLabel, "runpod-pods-empty").isHidden()


def test_remove_failure_shows_the_store_message(qtbot, make_panel, client):
    store = FakeStore(FAKE_KEY)
    panel = make_panel(store, client)
    store.failure = SecretStoreError("keyring delete failed (OSError)")
    press(qtbot, panel, "runpod-remove-key")
    assert text_of(panel, "runpod-result") == "keyring delete failed (OSError)"
    assert store.value == FAKE_KEY


# ----------------------------------------------------------------- test key


def test_test_key_does_not_call_meridian_when_the_server_is_not_running(qtbot, make_panel, store, client):
    client.state = HEALTH_NOT_RUNNING
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-test-key")
    assert [call[0] for call in client.calls] == ["probe_health"]
    assert text_of(panel, "runpod-result") == "Request not sent: the local Meridian server is not running."
    assert text_of(panel, "runpod-server-status") == (
        "Local Meridian server: not running at http://127.0.0.1:7878. Start Meridian on this computer."
    )


def test_test_key_reports_when_something_else_answers(qtbot, make_panel, store, client):
    client.state = HEALTH_NOT_MERIDIAN
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-test-key")
    assert text_of(panel, "runpod-result") == "Request not sent: the server at the local address is not Meridian."
    assert text_of(panel, "runpod-server-status") == (
        "Local Meridian server: something answers at http://127.0.0.1:7878, but it is not Meridian."
    )


def test_test_key_reports_a_probe_error(qtbot, make_panel, store, client):
    client.state = HEALTH_ERROR
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-test-key")
    assert text_of(panel, "runpod-result") == "Request not sent: the local Meridian server could not be checked."
    assert text_of(panel, "runpod-server-status") == (
        "Local Meridian server: could not be checked at http://127.0.0.1:7878."
    )


def test_test_key_reports_success_with_the_key_source(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-test-key")
    assert text_of(panel, "runpod-result") == (
        "RunPod key works. Key source: keyring. Billing: available. Pods on the first page: 2."
    )
    assert FAKE_KEY not in all_text(panel)


def test_test_key_shows_meridian_failure_text_as_plain_words(qtbot, make_panel, store, client):
    client.check = {
        "ok": False,
        "credential_source": None,
        "pod_count": 0,
        "billing_available": False,
        "error": {"code": "no_credential", "message": "No RunPod API key was found in the sources tried."},
    }
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-test-key")
    assert text_of(panel, "runpod-result") == (
        "RunPod key check failed: No RunPod API key was found in the sources tried."
    )


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("local_only", "Meridian accepts RunPod requests only from this computer"),
        ("http_404", "This Meridian server has no RunPod routes (HTTP 404)."),
        ("timeout", "The local Meridian server did not answer in time."),
        ("not_running", "The local Meridian server is not running."),
    ],
)
def test_test_key_maps_local_error_codes_to_fixed_text(qtbot, make_panel, store, client, code, expected):
    client.raise_on["runpod_check"] = LocalClientError(code, "SENTINEL-BODY-TEXT")
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-test-key")
    shown = text_of(panel, "runpod-result")
    assert shown.startswith(expected)
    assert "SENTINEL-BODY-TEXT" not in all_text(panel)


def test_test_key_uses_the_local_url_from_settings(qtbot, make_panel, store, client, settings_path):
    panel = make_panel(store, client)
    SettingsStore(settings_path).save(Settings(local_url="http://127.0.0.1:9999"))
    press(qtbot, panel, "runpod-test-key")
    assert client.calls[-1][1] == "http://127.0.0.1:9999"


# ----------------------------------------------------------------- refresh pods


# ----------------------------------------------------------------- GPU offers

OFFER_A = {
    "gpu_model": "rtx-4090-24gb",
    "provider": "runpod",
    "cloud_type": "secure",
    "data_center": "US-TX-3",
    "stock_status": "HIGH",
    "price_per_gpu_hour": 0.69,
    "price_per_hour": 0.69,
    "vram_gb_per_gpu": 24.0,
    "vcpu": 16,
    "ram_gb": 62.0,
}
OFFER_B = {
    "gpu_model": "a100-80gb",
    "provider": "runpod",
    "cloud_type": "community",
    "data_center": None,
    "stock_status": None,
    "price_per_gpu_hour": None,
    "price_per_hour": None,
    "vram_gb_per_gpu": 80.0,
    "vcpu": None,
    "ram_gb": None,
}
OFFERS_OK = {
    "ok": True,
    "status": "ok",
    "count": 2,
    "offers": [OFFER_A, OFFER_B],
    "providers": [
        {
            "name": "runpod",
            "status": "ok",
            "prices_as_of": "2026-11-10 04:00 UTC",
            "stock_as_of": "2026-11-10 04:01 UTC",
            "error": None,
        }
    ],
    "error": None,
}


def test_offer_row_reads_unknown_values_as_not_reported_never_zero():
    assert offer_row(OFFER_A) == (
        "rtx-4090-24gb", "runpod", "secure", "US-TX-3", "HIGH", "$0.69", "$0.69", "24 GB", "16", "62 GB",
    )
    assert offer_row(OFFER_B) == (
        "a100-80gb", "runpod", "community", "not reported", "not reported", "not reported",
        "not reported", "80 GB", "not reported", "not reported",
    )


def test_offer_sort_value_puts_an_unknown_price_last():
    assert offer_sort_value(OFFER_A, 5) == 0.69
    assert offer_sort_value(OFFER_B, 5) == float("inf")


def test_offer_query_sends_only_the_filters_that_are_set():
    query, problem = offer_query("  4090 ", "secure", 2, True, "0.75")
    assert problem is None
    assert query == {
        "gpu_count": "2",
        "gpu_model": "4090",
        "cloud_type": "secure",
        "in_stock_only": "true",
        "max_price_per_gpu_hour": "0.75",
    }
    assert offer_query("", None, 1, False, "") == ({"gpu_count": "1"}, None)


@pytest.mark.parametrize("cap", ["abc", "-1", "nan", "inf"])
def test_offer_query_refuses_a_bad_price_cap_and_sends_nothing(cap):
    assert offer_query("", None, 1, False, cap) == ({}, OFFER_CAP_INVALID_TEXT)


def test_offers_path_encodes_only_the_given_query():
    assert offers_path(None) == "/compute/offers"
    assert offers_path({}) == "/compute/offers"
    assert offers_path({"gpu_model": "rtx 4090"}) == "/compute/offers?gpu_model=rtx+4090"


def test_offer_status_and_error_code_are_read_from_the_payload():
    assert describe_offers(OFFERS_OK) == (
        "2 offer(s). Prices as of 2026-11-10 04:00 UTC. Stock as of 2026-11-10 04:01 UTC."
    )
    assert describe_offers({"count": 0, "providers": []}) == "0 offer(s). Prices as of unknown. Stock as of unknown."
    assert offer_error_code({"error": {"code": "not_configured"}}) == "not_configured"
    assert offer_error_code({"error": "plain text"}) is None


def test_offers_refresh_sends_the_filters_and_fills_the_table(qtbot, make_panel, store, client):
    client.offers = copy.deepcopy(OFFERS_OK)
    panel = make_panel(store, client)
    panel.findChild(QLineEdit, "runpod-offers-model").setText("4090")
    panel.findChild(QCheckBox, "runpod-offers-stock").setChecked(True)
    press(qtbot, panel, "runpod-offers-refresh")
    wait_idle(qtbot, panel)
    assert client.offer_queries[-1] == {"gpu_count": "1", "gpu_model": "4090", "in_stock_only": "true"}
    table = panel.findChild(QTableWidget, "runpod-offers")
    assert table.rowCount() == 2
    assert _cells(table, 0) == offer_row(OFFER_A)
    assert text_of(panel, "runpod-offers-status") == describe_offers(OFFERS_OK)


def test_offers_with_no_key_say_to_restart_the_local_server(qtbot, make_panel, store, client):
    client.offers = {
        "ok": False,
        "status": "not_configured",
        "count": 0,
        "offers": [],
        "providers": [],
        "error": {"code": "not_configured", "message": "RunPod is not configured on this machine."},
    }
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-offers-refresh")
    wait_idle(qtbot, panel)
    assert text_of(panel, "runpod-offers-status") == OFFERS_NOT_CONFIGURED_TEXT
    assert panel.findChild(QTableWidget, "runpod-offers").rowCount() == 0


def test_a_bad_price_cap_shows_the_message_and_makes_no_offers_call(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    panel.findChild(QLineEdit, "runpod-offers-max").setText("cheap")
    press(qtbot, panel, "runpod-offers-refresh")
    assert text_of(panel, "runpod-offers-status") == OFFER_CAP_INVALID_TEXT
    assert not any(name == "runpod_offers" for name, _base, _timeout in client.calls)


def test_an_offers_failure_clears_the_table_and_says_why(qtbot, make_panel, store, client):
    client.raise_on["runpod_offers"] = LocalClientError("unreachable", "The local Meridian server did not answer.")
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-offers-refresh")
    wait_idle(qtbot, panel)
    assert panel.findChild(QTableWidget, "runpod-offers").rowCount() == 0
    assert text_of(panel, "runpod-offers-status") != ""


def _cells(table, row):
    return tuple(table.item(row, column).text() for column in range(table.columnCount()))


def test_refresh_fills_the_table_with_the_pod_summaries(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-refresh-pods")
    table = panel.findChild(QTableWidget, "runpod-pods")
    headers = tuple(table.horizontalHeaderItem(column).text() for column in range(table.columnCount()))
    assert headers == POD_COLUMNS
    assert table.rowCount() == 2
    assert _cells(table, 0) == ("pod-a1", "trainer", "RUNNING", "NVIDIA RTX A4000 x1", "0.34", "7200")
    assert _cells(table, 1) == ("pod-b2", "idle-box", "EXITED", "", "0.0", "")
    assert text_of(panel, "runpod-result") == "Listed 2 pod(s)."


def test_refresh_says_when_more_pods_exist(qtbot, make_panel, store, client):
    client.listing["has_more_pods"] = True
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-refresh-pods")
    assert text_of(panel, "runpod-result") == "Listed 2 pod(s). More pods exist than this page shows."


def test_refresh_failure_clears_the_table_and_says_why(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-refresh-pods")
    client.listing = {
        "ok": False,
        "credential_source": None,
        "pods": [],
        "pod_count": 0,
        "has_more_pods": False,
        "error": {"code": "unauthorized", "message": "RunPod rejected the API key (HTTP 401)."},
    }
    press(qtbot, panel, "runpod-refresh-pods")
    assert panel.findChild(QTableWidget, "runpod-pods").rowCount() == 0
    assert text_of(panel, "runpod-result") == "RunPod pod list failed: RunPod rejected the API key (HTTP 401)."


def test_refresh_with_server_down_clears_the_table(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-refresh-pods")
    client.state = HEALTH_NOT_RUNNING
    press(qtbot, panel, "runpod-refresh-pods")
    assert panel.findChild(QTableWidget, "runpod-pods").rowCount() == 0
    assert "not running" in text_of(panel, "runpod-result")


def test_refresh_local_error_clears_the_table(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-refresh-pods")
    client.raise_on["runpod_list_pods"] = LocalClientError("timeout", "slow")
    press(qtbot, panel, "runpod-refresh-pods")
    assert panel.findChild(QTableWidget, "runpod-pods").rowCount() == 0
    assert text_of(panel, "runpod-result") == "The local Meridian server did not answer in time."


def test_pod_names_and_messages_are_plain_text_not_html(qtbot, make_panel, store, client):
    client.listing["pods"] = [dict(POD_A, name="<b>bold</b>")]
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-refresh-pods")
    table = panel.findChild(QTableWidget, "runpod-pods")
    assert table.item(0, 1).text() == "<b>bold</b>"

    client.check = {"ok": False, "error": {"code": "x", "message": "<i>not italic</i>"}}
    press(qtbot, panel, "runpod-test-key")
    result = panel.findChild(QLabel, "runpod-result")
    assert result.textFormat() == Qt.TextFormat.PlainText
    assert result.text() == "RunPod key check failed: <i>not italic</i>"


def test_an_unexpected_exception_is_reported_without_its_text(qtbot, make_panel, store, client):
    client.raise_on["runpod_check"] = RuntimeError(f"leaked {FAKE_KEY}")
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-test-key")
    assert text_of(panel, "runpod-result") == "Unexpected RuntimeError. Details were not shown."
    assert FAKE_KEY not in all_text(panel)


# ----------------------------------------------------------------- threading and busy state


def test_every_client_call_runs_off_the_ui_thread(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    press(qtbot, panel, "runpod-test-key")
    press(qtbot, panel, "runpod-refresh-pods")
    assert client.threads
    assert all(thread is not threading.main_thread() for thread in client.threads)


def test_buttons_are_disabled_while_a_job_runs(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    client.gate = threading.Event()
    test_button = panel.findChild(QPushButton, "runpod-test-key")

    test_button.click()

    assert panel.busy
    assert not test_button.isEnabled()
    client.gate.set()
    wait_idle(qtbot, panel)
    assert test_button.isEnabled()


def test_idle_is_emitted_when_work_finishes(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    with qtbot.waitSignal(panel.idle, timeout=5000):
        panel.findChild(QPushButton, "runpod-refresh-pods").click()


def test_showing_the_panel_probes_the_server_once(qtbot, make_panel, store, client):
    panel = make_panel(store, client)
    panel.show()
    qtbot.waitUntil(lambda: text_of(panel, "runpod-server-status").startswith("Local Meridian server: running"), timeout=5000)
    panel.hide()
    panel.show()
    wait_idle(qtbot, panel)
    assert [call[0] for call in client.calls].count("probe_health") == 1


def test_a_job_still_running_when_the_panel_is_deleted_does_not_keep_the_panel(qtbot, settings_path, monkeypatch):
    """A panel deleted while one of its jobs still runs is freed on the GUI thread, because the job holds no panel.

    The job used to hold the panel through its bound methods and its signal emit. When the pool ended the job on a
    worker, the worker dropped the last reference, so the panel could be freed off the GUI thread. The collector is
    off here, so only reference counting can free the panel. Nothing reaches the panel after it is gone.
    """
    gate = threading.Event()
    client = FakeClient()
    client.gate = gate
    delivered = []
    monkeypatch.setattr(RunPodPanel, "_on_test_done", lambda self, result, error: delivered.append(error))
    SettingsStore(settings_path).save(Settings(local_url=LOCAL_URL))

    gc.disable()
    try:
        container = QWidget()
        panel = RunPodPanel(SettingsStore(settings_path), FakeStore(FAKE_KEY), client, parent=container)
        wait_idle(qtbot, panel)  # the key-storage read made at build time has finished
        panel.findChild(QPushButton, "runpod-test-key").click()
        qtbot.waitUntil(lambda: any(call[0] == "probe_health" for call in client.calls), timeout=5000)  # held at the gate
        ref = weakref.ref(panel)
        shiboken6.delete(container)  # the panel's widgets are deleted on the GUI thread while the job still runs
        del panel
        assert ref() is None
        gate.set()
        assert QThreadPool.globalInstance().waitForDone(5000)
        qtbot.wait(50)  # the late result reaches the event loop, and nothing raises
        assert delivered == []
    finally:
        gate.set()
        gc.enable()


# ------------------------------------------------- a result for another owner, or for a job already finished


def test_a_result_from_another_owner_or_for_a_job_already_finished_is_dropped(qtbot, make_panel, store):
    """A sibling panel's answer, and an answer for a job this panel has already finished, show nothing.

    This page has no generation. Each job has its own id, per panel, and the panel keeps a result only for a job id it
    is still waiting for. Here the panel's first job is held at the gate while the sibling's first job, which has the
    same job id, finishes. The sibling's answer reaches the panel and must be dropped by owner alone, while the panel
    still waits for its own job. Then the panel's job finishes, and a second job runs. The first job's answer is replayed:
    the panel has finished that job id, so it is dropped by job id alone. The second job's result is the control that
    the panel's own current answer is accepted.
    """
    gate = threading.Event()
    held = FakeClient()
    held.gate = gate
    panel = make_panel(store, held)
    sibling = make_panel(store, FakeClient())
    try:
        with recording_deliveries() as emitted:
            panel.findChild(QPushButton, "runpod-test-key").click()
            qtbot.waitUntil(lambda: any(call[0] == "probe_health" for call in held.calls), timeout=5000)  # at the gate
            waiting = visible_state(panel)
            press(qtbot, sibling, "runpod-test-key")
            assert len(emitted) == 1
            assert visible_state(panel) == waiting
            gate.set()
            wait_idle(qtbot, panel)
            assert len(emitted) == 2
            own_first = emitted[1]
            shown_first = visible_state(panel)
            held.state = HEALTH_NOT_MERIDIAN
            press(qtbot, panel, "runpod-test-key")
            shown_second = visible_state(panel)
            assert shown_second != shown_first
            emit(own_first)  # this panel's answer for a job it has already finished
            qtbot.wait(20)
    finally:
        gate.set()  # a failed assertion must not leave the held job blocked after the test ends
    assert visible_state(panel) == shown_second
