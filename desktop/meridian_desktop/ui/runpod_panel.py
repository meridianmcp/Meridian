"""RunPod panel for the Keys tab: key storage, a key check, the pod list and the server state.

This is Qt code, and the only new Qt module. It sits in ui/, so the boundary check allows it.

The panel never shows the key. It stores the key through the secret store, and Meridian
reads it from the keychain on each check. Every keychain call and every loopback call
runs on the thread pool, so the UI never blocks. Results and errors are plain text
(PlainText labels, plain table cells), so server text is never read as HTML.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import local_client
from meridian_desktop.local_client import LocalClientError
from meridian_desktop.secret_store import RUNPOD_ACCOUNT, RUNPOD_SERVICE, SecretStore, SecretStoreError
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.confirm import ask_yes_no
from meridian_desktop.ui.delivery import Delivered, Delivery, shared_delivery
from meridian_desktop.ui.text import enable_table_copy, make_selectable

PROBE_TIMEOUT = 2.0
RUNPOD_TIMEOUT = 30.0
POD_COLUMNS = ("id", "name", "status", "gpu", "costPerHour", "uptimeSeconds")
MAX_SERVER_TEXT = 300
# The pods table keeps room for a few rows inside the Keys section, so the card never collapses to a sliver.
PODS_MIN_HEIGHT = 96
PODS_MAX_HEIGHT = 160

KEY_STORED_TEXT = "A RunPod key is stored in the keychain."
KEY_MISSING_TEXT = "No RunPod key is stored."
REMOVE_KEY_TITLE = "Remove the RunPod key?"
REMOVE_KEY_BODY = "The RunPod key is deleted from the keychain. This cannot be undone."
REMOVE_KEY_CANCELLED_TEXT = "Not removed. The RunPod key is still saved."
# A removal that failed on the disk. The message names no path and no system text.
REMOVE_KEY_FAILED_TEXT = "Not removed. The RunPod key could not be deleted from the keychain. Try again."
NOTHING_HERE_TEXT = "Nothing here yet."
NOT_REPORTED_TEXT = "not reported"
OFFER_COLUMNS = ("GPU model", "Provider", "Cloud", "Data center", "Stock", "$ / GPU-hr", "$ / machine-hr", "VRAM / GPU", "vCPU", "RAM")
OFFER_CLOUD_CHOICES: tuple[tuple[str, str | None], ...] = (
    ("All clouds", None),
    ("Secure", "secure"),
    ("Community", "community"),
    ("Spot", "spot"),
)
OFFER_GPU_COUNT_CHOICES: tuple[int, ...] = (1, 2, 4, 8)
# Column index -> the offer field that sorts that column as a number. An unknown value sorts last.
OFFER_SORT_KEYS: dict[int, str] = {
    5: "price_per_gpu_hour",
    6: "price_per_hour",
    7: "vram_gb_per_gpu",
    8: "vcpu",
    9: "ram_gb",
}
OFFERS_TITLE_TEXT = "GPU offers: RunPod list prices and stock. Read only; nothing here starts or bills a pod."
OFFERS_UNCHECKED_TEXT = "GPU offers not loaded yet. Press Refresh offers."
OFFERS_EMPTY_TEXT = "No offers to show. See the status above."
OFFERS_NOT_CONFIGURED_TEXT = (
    "RunPod is not configured for the local Meridian server. Save the key, restart the local Meridian server "
    "so it reads RUNPOD_API_KEY, then refresh."
)
OFFER_CAP_INVALID_TEXT = "Max $ / GPU-hr must be a number of 0 or more."
SERVER_UNCHECKED_TEXT = "Local Meridian server: not checked yet"


def _truncate(text: str, limit: int = MAX_SERVER_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def describe_local_error(error: LocalClientError) -> str:
    """Plain text for a failed local request. It uses this module's wording only."""
    code = error.code
    if code == "not_running":
        return "The local Meridian server is not running. Start Meridian on this computer, then try again."
    if code == "timeout":
        return "The local Meridian server did not answer in time."
    if code == "unreachable":
        return "The local Meridian server could not be reached."
    if code == "not_loopback":
        return "The local server address in Settings is not a loopback address, so no request was sent."
    if code == "local_only":
        return "Meridian accepts RunPod requests only from this computer, and it refused this one."
    if code == "hosted_refused":
        return "This Meridian server is hosted. RunPod checks run only on a self-hosted Meridian."
    if code == "http_404":
        return "This Meridian server has no RunPod routes (HTTP 404). Update Meridian to a build that includes them."
    if code == "redirect_refused":
        return "The local Meridian server sent a redirect, which the desktop does not follow."
    if code == "too_large":
        return "The local Meridian server sent a response that is too large to read."
    if code == "bad_json":
        return "The local Meridian server sent a response the desktop could not read."
    if code.startswith("http_"):
        return f"The local Meridian server returned HTTP {code[len('http_'):]}."
    return "The local Meridian server returned an error."


def not_sent_text(state: str | None) -> str:
    """The result line when the local server is not running as Meridian, so nothing was sent."""
    if state == local_client.HEALTH_NOT_RUNNING:
        return "Request not sent: the local Meridian server is not running."
    if state == local_client.HEALTH_NOT_MERIDIAN:
        return "Request not sent: the server at the local address is not Meridian."
    return "Request not sent: the local Meridian server could not be checked."


def server_state_text(state: str | None, url: str) -> str:
    """The one-line local server status, from a probe_health result."""
    if state == local_client.HEALTH_RUNNING:
        return f"Local Meridian server: running at {url}"
    if state == local_client.HEALTH_NOT_RUNNING:
        return f"Local Meridian server: not running at {url}. Start Meridian on this computer."
    if state == local_client.HEALTH_NOT_MERIDIAN:
        return f"Local Meridian server: something answers at {url}, but it is not Meridian."
    if state == local_client.HEALTH_ERROR:
        return f"Local Meridian server: could not be checked at {url}."
    return SERVER_UNCHECKED_TEXT


def _cell(value: Any) -> str:
    return "" if value is None else str(value)


def pod_row(pod: dict[str, Any]) -> tuple[str, ...]:
    """Cells for one pod, in POD_COLUMNS order. A missing value is an empty cell."""
    gpu = pod.get("gpu")
    gpu_text = ""
    if isinstance(gpu, dict) and gpu.get("id") is not None:
        gpu_text = str(gpu["id"]) if gpu.get("count") is None else f"{gpu['id']} x{gpu['count']}"
    return (
        _cell(pod.get("id")),
        _cell(pod.get("name")),
        _cell(pod.get("status")),
        gpu_text,
        _cell(pod.get("costPerHour")),
        _cell(pod.get("uptimeSeconds")),
    )


def _server_error_text(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    message = error.get("message") if isinstance(error, dict) else None
    if isinstance(message, str) and message:
        return _truncate(message)
    return "Meridian did not say why the check failed."


def describe_check(payload: dict[str, Any]) -> str:
    if payload.get("ok") is not True:
        return f"RunPod key check failed: {_server_error_text(payload)}"
    source = payload.get("credential_source") or "not reported"
    billing = "available" if payload.get("billing_available") else "not available"
    return (
        f"RunPod key works. Key source: {source}. Billing: {billing}. "
        f"Pods on the first page: {payload.get('pod_count', 0)}."
    )


def describe_pod_list(payload: dict[str, Any], pods: list[dict[str, Any]]) -> str:
    text = f"Listed {len(pods)} pod(s)."
    if payload.get("has_more_pods") is True:
        text += " More pods exist than this page shows."
    return text


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _usd(value: Any) -> str:
    number = _number(value)
    return NOT_REPORTED_TEXT if number is None else f"${number:,.2f}"


def _gigabytes(value: Any) -> str:
    number = _number(value)
    return NOT_REPORTED_TEXT if number is None else f"{number:g} GB"


def _plain(value: Any) -> str:
    if value is None or value == "":
        return NOT_REPORTED_TEXT
    return _truncate(str(value))


def offer_row(offer: dict[str, Any]) -> tuple[str, ...]:
    """The table cells for one offer, as plain text. An unknown value reads 'not reported', never zero."""
    vcpu = _number(offer.get("vcpu"))
    return (
        _plain(offer.get("gpu_model")),
        _plain(offer.get("provider")),
        _plain(offer.get("cloud_type")),
        _plain(offer.get("data_center")),
        _plain(offer.get("stock_status")),
        _usd(offer.get("price_per_gpu_hour")),
        _usd(offer.get("price_per_hour")),
        _gigabytes(offer.get("vram_gb_per_gpu")),
        NOT_REPORTED_TEXT if vcpu is None else f"{vcpu:g}",
        _gigabytes(offer.get("ram_gb")),
    )


def offer_sort_value(offer: dict[str, Any], column: int) -> float:
    """The number a numeric column sorts by. An unknown value is infinity, so it sorts last."""
    number = _number(offer.get(OFFER_SORT_KEYS[column]))
    return math.inf if number is None else number


def offer_query(model: str, cloud: str | None, gpus: int, in_stock: bool, max_price: str) -> tuple[dict[str, str], str | None]:
    """The GET /compute/offers filters for the form. A problem text means the form is invalid, and nothing is sent."""
    query: dict[str, str] = {"gpu_count": str(gpus)}
    name = model.strip()
    if name:
        query["gpu_model"] = name
    if cloud:
        query["cloud_type"] = cloud
    if in_stock:
        query["in_stock_only"] = "true"
    cap = max_price.strip()
    if cap:
        try:
            value = float(cap)
        except ValueError:
            return {}, OFFER_CAP_INVALID_TEXT
        if not math.isfinite(value) or value < 0:
            return {}, OFFER_CAP_INVALID_TEXT
        query["max_price_per_gpu_hour"] = repr(value)
    return query, None


def offer_error_code(payload: dict[str, Any]) -> str | None:
    error = payload.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def describe_offers(payload: dict[str, Any]) -> str:
    """The status line under the filters: how many offers, and when prices and stock were read."""
    raw = payload.get("providers")
    providers = [p for p in raw if isinstance(p, dict)] if isinstance(raw, list) else []
    prices = next((p["prices_as_of"] for p in providers if p.get("prices_as_of")), None)
    stock = next((p["stock_as_of"] for p in providers if p.get("stock_as_of")), None)
    count = payload.get("count") if isinstance(payload.get("count"), int) else 0
    return f"{count} offer(s). Prices as of {prices or 'unknown'}. Stock as of {stock or 'unknown'}."


class _SortItem(QTableWidgetItem):
    """A table cell that sorts by its UserRole number when it has one, so prices sort as numbers."""

    def __lt__(self, other: object) -> bool:
        # Compare here, not through super(): on PySide6 a super() call back into this override recurses forever.
        if not isinstance(other, QTableWidgetItem):
            return False
        mine = self.data(Qt.ItemDataRole.UserRole)
        theirs = other.data(Qt.ItemDataRole.UserRole)
        if isinstance(mine, float) and isinstance(theirs, float):
            return mine < theirs
        return self.text() < other.text()


@dataclass
class _Outcome:
    """What one local job found: the probed state, a payload, or a failure text."""

    url: str
    state: str | None = None
    payload: dict[str, Any] | None = None
    failure: str | None = None


@dataclass(frozen=True)
class _JobDone:
    """One finished job, as it is handed back: its job id, the callable's result, and the error text if it failed."""

    job_id: int
    result: Any
    error: str | None


class _Work:
    """The panel's work on the thread pool: the keychain calls and the loopback calls, through the stores and client it was given.

    It holds no widget. A job that holds one of these methods never keeps the panel alive, and never touches it. The
    panel holds one of these and hands its methods to the thread pool.
    """

    def __init__(self, settings_store: SettingsStore, secret_store: SecretStore, client: Any) -> None:
        self._settings = settings_store
        self._secrets = secret_store
        self._client = client

    def read_key_present(self) -> bool:
        """True when a key is stored. The value stays in the worker and is never returned."""
        return self._secrets.get_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT) is not None

    def set_key(self, value: str) -> Any:
        return self._secrets.set_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT, value)

    def delete_key(self) -> Any:
        try:
            return self._secrets.delete_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT)
        except OSError:
            # Reduced to the plain line here, on the worker, so no path or system text reaches the panel.
            raise SecretStoreError(REMOVE_KEY_FAILED_TEXT) from None

    def local(self, fetch: Callable[[str, float], dict[str, Any]] | None) -> _Outcome:
        """Probe the local server, then run ``fetch`` only when it is running. Runs on a worker."""
        url = self._settings.load().settings.local_url
        state = self._client.probe_health(url, PROBE_TIMEOUT)
        outcome = _Outcome(url=url, state=state)
        if fetch is not None and state == local_client.HEALTH_RUNNING:
            try:
                outcome.payload = fetch(url, RUNPOD_TIMEOUT)
            except LocalClientError as exc:
                outcome.failure = describe_local_error(exc)
        return outcome

    def probe_only(self) -> _Outcome:
        return self.local(None)


class _Job(QRunnable):
    """Run one callable on the thread pool and deliver a _JobDone to the shared delivery object.

    The job holds only the callable, the owner token, its job id and the shared delivery object. It never holds the
    panel. The error text is produced here, in the worker, so a raw exception never reaches the UI thread. An
    exception that could hold a value is reduced to its class name.
    """

    def __init__(self, job_id: int, work: Callable[[], Any], owner: object, delivery: Delivery) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._job_id = job_id
        self._work = work
        self._owner = owner
        self._delivery = delivery

    def run(self) -> None:
        result: Any = None
        error: str | None = None
        try:
            result = self._work()
        except LocalClientError as exc:
            error = describe_local_error(exc)
        except SecretStoreError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            error = f"Unexpected {exc.__class__.__name__}. Details were not shown."
        self._delivery.delivered.emit(Delivered(self._owner, _JobDone(self._job_id, result, error)))


def _label(parent: QWidget, name: str, text: str) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


class RunPodPanel(QWidget):
    """Keys tab content: key storage, a key check, the pod list and the server state. Its entry card is placed by the Keys tab."""

    #: Emitted when no background job is running.
    idle = Signal()

    @property
    def busy(self) -> bool:
        """True while a keychain or loopback job is running."""
        return self._pending > 0

    def __init__(
        self,
        settings_store: SettingsStore,
        secret_store: SecretStore,
        client: Any = local_client,
        parent: QWidget | None = None,
        *,
        confirm: Callable[[QWidget, str, str], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("runpod-panel")
        self._client = client
        # The Yes or No before the RunPod key is removed. Tests pass a fake; the app uses the plain dialog.
        self._confirm = confirm if confirm is not None else ask_yes_no
        self._work = _Work(settings_store, secret_store, client)
        # Identifies this panel's jobs. A plain object, so a job can hold it without holding the panel.
        self._owner = object()
        self._job_ids = itertools.count(1)
        # Each waiting job's handler, as an unbound function. A bound method would make the panel hold itself.
        self._handlers: dict[int, Callable[[RunPodPanel, Any, str | None], None]] = {}
        self._pending = 0
        self._probed_on_show = False
        shared_delivery().delivered.connect(self._on_delivered)
        self._build()
        self._submit(self._work.read_key_present, RunPodPanel._on_key_checked)

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        # Every row of the panel sits in one entry card. The Keys tab's section takes the card with take_card().
        self._card = QWidget(self)
        self._card.setObjectName("runpod-entry-card")
        layout = QVBoxLayout(self._card)
        layout.setContentsMargins(0, 0, 0, 0)

        self._server_status = _label(self, "runpod-server-status", SERVER_UNCHECKED_TEXT)
        layout.addWidget(self._server_status)

        self._key_status = _label(self, "runpod-key-status", "Checking key storage...")
        layout.addWidget(self._key_status)

        key_row = QHBoxLayout()
        self._key_input = QLineEdit(self)
        self._key_input.setObjectName("runpod-key-input")
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText("RunPod API key")
        key_row.addWidget(self._key_input, 1)
        self._save_button = QPushButton("Save key", self)
        self._save_button.setObjectName("runpod-save-key")
        key_row.addWidget(self._save_button)
        self._remove_button = QPushButton("Remove key", self)
        self._remove_button.setObjectName("runpod-remove-key")
        key_row.addWidget(self._remove_button)
        layout.addLayout(key_row)

        action_row = QHBoxLayout()
        self._test_button = QPushButton("Test key", self)
        self._test_button.setObjectName("runpod-test-key")
        action_row.addWidget(self._test_button)
        self._refresh_button = QPushButton("Refresh pods", self)
        self._refresh_button.setObjectName("runpod-refresh-pods")
        action_row.addWidget(self._refresh_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        self._result = _label(self, "runpod-result", "")
        # The result line takes no room until it has text, so an empty line never leaves a gap above the pods table.
        self._result.setVisible(False)
        layout.addWidget(self._result)

        # With no pods the grid is hidden and one plain line says so, so an empty box never shows.
        self._pods_empty = make_selectable(QLabel(NOTHING_HERE_TEXT, self))
        self._pods_empty.setObjectName("runpod-pods-empty")
        layout.addWidget(self._pods_empty)
        self._pods = QTableWidget(0, len(POD_COLUMNS), self)
        self._pods.setObjectName("runpod-pods")
        self._pods.setVisible(False)
        self._pods.setMinimumHeight(PODS_MIN_HEIGHT)
        self._pods.setMaximumHeight(PODS_MAX_HEIGHT)
        self._pods.setHorizontalHeaderLabels(list(POD_COLUMNS))
        self._pods.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._pods.verticalHeader().setVisible(False)
        header = self._pods.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        enable_table_copy(self._pods)
        layout.addWidget(self._pods, 1)
        self._build_offers(layout)
        outer.addWidget(self._card)

        self._save_button.clicked.connect(self._on_save_clicked)
        self._remove_button.clicked.connect(self._on_remove_clicked)
        self._test_button.clicked.connect(self._on_test_clicked)
        self._refresh_button.clicked.connect(self._on_refresh_clicked)

    def _build_offers(self, layout: QVBoxLayout) -> None:
        """The GPU offers section: filters, a refresh, the status line and a sortable table of RunPod list prices."""
        layout.addWidget(_label(self, "runpod-offers-title", OFFERS_TITLE_TEXT))
        filters = QHBoxLayout()
        self._offer_model = QLineEdit(self)
        self._offer_model.setObjectName("runpod-offers-model")
        self._offer_model.setPlaceholderText("GPU model, for example 4090")
        filters.addWidget(self._offer_model, 1)
        self._offer_cloud = QComboBox(self)
        self._offer_cloud.setObjectName("runpod-offers-cloud")
        for label, value in OFFER_CLOUD_CHOICES:
            self._offer_cloud.addItem(label, value)
        filters.addWidget(self._offer_cloud)
        self._offer_gpus = QComboBox(self)
        self._offer_gpus.setObjectName("runpod-offers-gpus")
        for count in OFFER_GPU_COUNT_CHOICES:
            self._offer_gpus.addItem(f"{count} GPU" + ("s" if count > 1 else ""), count)
        filters.addWidget(self._offer_gpus)
        self._offer_stock = QCheckBox("In stock only", self)
        self._offer_stock.setObjectName("runpod-offers-stock")
        filters.addWidget(self._offer_stock)
        self._offer_max = QLineEdit(self)
        self._offer_max.setObjectName("runpod-offers-max")
        self._offer_max.setPlaceholderText("Max $ / GPU-hr")
        filters.addWidget(self._offer_max)
        self._offer_refresh = QPushButton("Refresh offers", self)
        self._offer_refresh.setObjectName("runpod-offers-refresh")
        filters.addWidget(self._offer_refresh)
        layout.addLayout(filters)

        self._offer_status = _label(self, "runpod-offers-status", OFFERS_UNCHECKED_TEXT)
        layout.addWidget(self._offer_status)
        self._offers_empty = make_selectable(QLabel(OFFERS_EMPTY_TEXT, self))
        self._offers_empty.setObjectName("runpod-offers-empty")
        layout.addWidget(self._offers_empty)
        self._offers = QTableWidget(0, len(OFFER_COLUMNS), self)
        self._offers.setObjectName("runpod-offers")
        self._offers.setVisible(False)
        self._offers.setMinimumHeight(PODS_MIN_HEIGHT)
        self._offers.setMaximumHeight(PODS_MAX_HEIGHT)
        self._offers.setHorizontalHeaderLabels(list(OFFER_COLUMNS))
        self._offers.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._offers.setSortingEnabled(True)
        self._offers.verticalHeader().setVisible(False)
        header = self._offers.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        enable_table_copy(self._offers)
        layout.addWidget(self._offers, 1)
        self._offer_refresh.clicked.connect(self._on_offers_clicked)

    def _set_busy(self, busy: bool) -> None:
        for widget in (
            self._save_button,
            self._remove_button,
            self._test_button,
            self._refresh_button,
            self._key_input,
            self._offer_refresh,
            self._offer_model,
            self._offer_cloud,
            self._offer_gpus,
            self._offer_stock,
            self._offer_max,
        ):
            widget.setEnabled(not busy)

    # ----------------------------------------------------------- jobs

    def _submit(self, work: Callable[[], Any], handler: Callable[[RunPodPanel, Any, str | None], None]) -> None:
        job_id = next(self._job_ids)
        self._handlers[job_id] = handler
        self._pending += 1
        self._set_busy(True)
        # The job holds the callable, the owner token, its job id and the shared delivery object. It never holds the panel.
        QThreadPool.globalInstance().start(_Job(job_id, work, self._owner, shared_delivery()))

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another panel is dropped here, and so is one for a job this panel is not
        # waiting for. Every job has its own id, and each one's handler must run, so no newer job supersedes another.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        done = item.value
        if not isinstance(done, _JobDone) or done.job_id not in self._handlers:
            return
        handler = self._handlers.pop(done.job_id)
        self._pending -= 1
        handler(self, done.result, done.error)
        self._set_busy(self._pending > 0)
        if self._pending == 0:
            self.idle.emit()

    def _set_result(self, text: str) -> None:
        """Show the result line with this text. An empty text hides the line, so it leaves no gap."""
        self._result.setText(text)
        self._result.setVisible(bool(text))

    def _take(self, result: Any, error: str | None) -> _Outcome | None:
        """Return the job's outcome, or show the error and return None."""
        if error is not None or not isinstance(result, _Outcome):
            self._set_result(error or "No result was produced.")
            return None
        if result.state is not None:
            self._server_status.setText(server_state_text(result.state, result.url))
        return result

    def _on_key_checked(self, result: Any, error: str | None) -> None:
        if error is not None:
            self._key_status.setText(f"Key storage could not be read. {error}")
            return
        self._key_status.setText(KEY_STORED_TEXT if result else KEY_MISSING_TEXT)

    # ----------------------------------------------------------- key actions

    @Slot()
    def _on_save_clicked(self) -> None:
        value = self._key_input.text()
        if not value.strip():
            self._set_result("Type a key first, then press Save key.")
            return
        work = self._work
        self._submit(lambda: work.set_key(value), RunPodPanel._on_save_done)

    def _on_save_done(self, result: Any, error: str | None) -> None:
        if error is not None:
            self._set_result(error)
            return
        self._key_input.clear()
        self._key_status.setText(KEY_STORED_TEXT)
        self._set_result("Key saved in the keychain.")

    @Slot()
    def _on_remove_clicked(self) -> None:
        # Removing the key takes the owner's yes first. No, or Escape, changes nothing.
        if not self._confirm(self, REMOVE_KEY_TITLE, REMOVE_KEY_BODY):
            self._set_result(REMOVE_KEY_CANCELLED_TEXT)
            return
        self._submit(self._work.delete_key, RunPodPanel._on_remove_done)

    def _on_remove_done(self, result: Any, error: str | None) -> None:
        if error is not None:
            self._set_result(error)
            return
        self._key_status.setText(KEY_MISSING_TEXT)
        self._set_result("Key removed from the keychain." if result else "No key was stored, so nothing was removed.")

    # ----------------------------------------------------------- Meridian actions

    @Slot()
    def _on_test_clicked(self) -> None:
        check = self._client.runpod_check
        work = self._work
        self._submit(lambda: work.local(check), RunPodPanel._on_test_done)

    def _on_test_done(self, result: Any, error: str | None) -> None:
        outcome = self._take(result, error)
        if outcome is None:
            return
        if outcome.failure is not None:
            self._set_result(outcome.failure)
        elif outcome.state != local_client.HEALTH_RUNNING:
            self._set_result(not_sent_text(outcome.state))
        else:
            self._set_result(describe_check(outcome.payload or {}))

    @Slot()
    def _on_refresh_clicked(self) -> None:
        listing = self._client.runpod_list_pods
        work = self._work
        self._submit(lambda: work.local(listing), RunPodPanel._on_refresh_done)

    def _on_refresh_done(self, result: Any, error: str | None) -> None:
        outcome = self._take(result, error)
        if outcome is None:
            return
        if outcome.failure is not None:
            self._clear_pods()
            self._set_result(outcome.failure)
            return
        if outcome.state != local_client.HEALTH_RUNNING:
            self._clear_pods()
            self._set_result(not_sent_text(outcome.state))
            return
        payload = outcome.payload or {}
        if payload.get("ok") is not True:
            self._clear_pods()
            self._set_result(f"RunPod pod list failed: {_server_error_text(payload)}")
            return
        raw = payload.get("pods")
        pods = [pod for pod in raw if isinstance(pod, dict)] if isinstance(raw, list) else []
        self._fill_pods(pods)
        self._set_result(describe_pod_list(payload, pods))

    def _fill_pods(self, pods: list[dict[str, Any]]) -> None:
        self._pods.setRowCount(len(pods))
        for row, pod in enumerate(pods):
            for column, cell in enumerate(pod_row(pod)):
                self._pods.setItem(row, column, QTableWidgetItem(cell))
        self._show_pod_rows(bool(pods))

    def _clear_pods(self) -> None:
        self._pods.setRowCount(0)
        self._show_pod_rows(False)

    def _show_pod_rows(self, has_rows: bool) -> None:
        """The grid shows when there are rows. Otherwise the one plain 'nothing here yet' line shows."""
        self._pods.setVisible(has_rows)
        self._pods_empty.setVisible(not has_rows)

    # ----------------------------------------------------------- GPU offers

    def _set_offer_status(self, text: str) -> None:
        self._offer_status.setText(text)

    def _on_offers_clicked(self) -> None:
        query, problem = offer_query(
            self._offer_model.text(),
            self._offer_cloud.currentData(),
            self._offer_gpus.currentData(),
            self._offer_stock.isChecked(),
            self._offer_max.text(),
        )
        if problem is not None:
            self._set_offer_status(problem)
            return
        offers = self._client.runpod_offers
        work = self._work
        self._submit(lambda: work.local(lambda url, timeout: offers(url, query, timeout)), RunPodPanel._on_offers_done)

    def _on_offers_done(self, result: Any, error: str | None) -> None:
        outcome = self._take(result, error)
        if outcome is None:
            return
        if outcome.failure is not None:
            self._clear_offers()
            self._set_offer_status(outcome.failure)
            return
        if outcome.state != local_client.HEALTH_RUNNING:
            self._clear_offers()
            self._set_offer_status(not_sent_text(outcome.state))
            return
        payload = outcome.payload or {}
        if payload.get("ok") is not True:
            self._clear_offers()
            not_configured = payload.get("status") == "not_configured" or offer_error_code(payload) == "not_configured"
            self._set_offer_status(
                OFFERS_NOT_CONFIGURED_TEXT if not_configured else f"RunPod offers failed: {_server_error_text(payload)}"
            )
            return
        raw = payload.get("offers")
        rows = [offer for offer in raw if isinstance(offer, dict)] if isinstance(raw, list) else []
        self._fill_offers(rows)
        self._set_offer_status(describe_offers(payload))

    def _fill_offers(self, offers: list[dict[str, Any]]) -> None:
        self._offers.setSortingEnabled(False)
        self._offers.setRowCount(len(offers))
        for row, offer in enumerate(offers):
            for column, cell in enumerate(offer_row(offer)):
                item = _SortItem(cell)
                if column in OFFER_SORT_KEYS:
                    item.setData(Qt.ItemDataRole.UserRole, offer_sort_value(offer, column))
                self._offers.setItem(row, column, item)
        self._offers.setSortingEnabled(True)
        self._show_offer_rows(bool(offers))

    def _clear_offers(self) -> None:
        self._offers.setRowCount(0)
        self._show_offer_rows(False)

    def _show_offer_rows(self, has_rows: bool) -> None:
        """The offers table shows when there are rows. Otherwise the one plain line says nothing matched."""
        self._offers.setVisible(has_rows)
        self._offers_empty.setVisible(not has_rows)

    # ----------------------------------------------------------- first show

    def take_card(self) -> QWidget:
        """Give the entry card to the host that places it, such as the Keys section. The panel keeps its jobs and signals.

        The panel is left with no rows of its own. Its first-show probe still runs through ensure_probed.
        """
        self.layout().removeWidget(self._card)
        return self._card

    def ensure_probed(self) -> None:
        """Probe the local server once, on the first call. A host that places the card calls this when it is shown."""
        if not self._probed_on_show:
            self._probed_on_show = True
            self._submit(self._work.probe_only, RunPodPanel._take_probe_only)

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self.ensure_probed()

    def _take_probe_only(self, result: Any, error: str | None) -> None:
        self._take(result, error)


__all__ = [
    "KEY_MISSING_TEXT",
    "KEY_STORED_TEXT",
    "POD_COLUMNS",
    "RunPodPanel",
    "describe_check",
    "describe_local_error",
    "describe_pod_list",
    "pod_row",
    "server_state_text",
]
