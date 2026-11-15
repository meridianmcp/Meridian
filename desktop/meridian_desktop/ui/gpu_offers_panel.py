"""RunPod GPU offers: the GPU types RunPod lists now, with stock, prices and data centers, and filters over them.

The panel makes no request when it is built. It reads once when it is first shown, and again when Check again is
pressed. The key is read and the request is made on the thread pool, so the UI thread never waits on the network.
A result from an older check is dropped. Text is plain (PlainText labels), so a reply is never read as HTML.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import QRunnable, Qt, QThreadPool, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import gpu_catalog
from meridian_desktop.gpu_catalog import (
    TIER_ANY,
    TIER_COMMUNITY,
    TIER_SECURE,
    DataCenterStock,
    GpuCatalogError,
    GpuOffer,
    filter_offers,
    sort_offers,
    tier_price,
)
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.text import make_selectable

UNCHECKED_TEXT = "RunPod GPU offers: not checked yet."
CHECKING_TEXT = "RunPod GPU offers: checking..."
FAILED_TEXT = "The check failed unexpectedly ({})."
COLUMNS = ("GPU", "Manufacturer", "Memory GB", "Stock", "Secure $/h", "Community $/h", "Max GPUs per pod", "Data centers")
TIER_CHOICES = (("Any cloud", TIER_ANY), ("Secure cloud", TIER_SECURE), ("Community cloud", TIER_COMMUNITY))
NOT_OFFERED_TEXT = "not offered"
ANY_MAKER_TEXT = "Any maker"
IN_STOCK_TEXT = "In stock only"
SORT_CHOICES = (
    ("Sort: name", "name"),
    ("Memory, most first", "memory_desc"),
    ("Memory, least first", "memory_asc"),
    ("Secure price, cheapest first", "secure_price"),
    ("Community price, cheapest first", "community_price"),
    ("Stock, most first", "stock"),
)
NO_DATA_CENTERS_TEXT = "none listed"


def money_text(value: float | None) -> str:
    """A price per GPU hour, or 'not offered' when the tier lists none."""
    return NOT_OFFERED_TEXT if value is None else f"${value:.2f}"


def counts_text(secure: int | None, community: int | None) -> str:
    """How many GPUs one pod can take in each tier. A cap of zero or no cap is left out."""
    parts = []
    if secure:
        parts.append(f"secure up to {secure}")
    if community:
        parts.append(f"community up to {community}")
    return ", ".join(parts) or "none"


def centers_text(centers: tuple[DataCenterStock, ...]) -> str:
    """Each data center with its stock level, in RunPod's order."""
    return ", ".join(f"{center.id} ({center.availability.lower()})" for center in centers) or NO_DATA_CENTERS_TEXT


def offer_cells(offer: GpuOffer) -> tuple[str, ...]:
    """The table cells for one offer, in COLUMNS order."""
    return (
        offer.name,
        offer.manufacturer,
        "" if offer.memory_gb is None else str(offer.memory_gb),
        offer.availability,
        money_text(tier_price(offer, TIER_SECURE)),
        money_text(tier_price(offer, TIER_COMMUNITY)),
        counts_text(offer.max_count_secure, offer.max_count_community),
        centers_text(offer.data_centers),
    )


def data_center_ids(offers: tuple[GpuOffer, ...]) -> list[str]:
    """Every data center that appears in the offers, sorted once."""
    return sorted({center.id for offer in offers for center in offer.data_centers})


def _label(parent: QWidget, name: str, text: str) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


class _CheckJob(QRunnable):
    """Run one catalog read on the thread pool and deliver (offers, failure_text).

    A GpuCatalogError carries fixed text. Any other exception is reduced to its class name here, in the worker.
    """

    def __init__(self, work: Callable[[], Any], deliver: Callable[[tuple], None]) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._work = work
        self._deliver = deliver

    def run(self) -> None:
        offers: Any = None
        failure: str | None = None
        try:
            offers = self._work()
        except GpuCatalogError as exc:
            failure = str(exc)
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            failure = FAILED_TEXT.format(exc.__class__.__name__)
        try:
            self._deliver((offers, failure))
        except RuntimeError:
            pass  # the shared delivery object is gone, for example at shutdown


class GpuOffersPanel(QWidget):
    """The RunPod offers list: a filter row over a table of the GPU types RunPod lists now."""

    def __init__(
        self,
        key_reader: Callable[[], str | None],
        fetch: Callable[[str | None], tuple[GpuOffer, ...]] = gpu_catalog.fetch_catalog,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("gpu-offers-panel")
        self._key_reader = key_reader
        self._fetch = fetch
        self._offers: tuple[GpuOffer, ...] = ()
        self._checked_text = ""
        self._probed_on_show = False
        self._checking = False
        # Identifies this panel's checks. A plain object, so a job can hold it without holding the panel.
        self._owner = object()
        # Each check takes the next number. A result with an older number is dropped.
        self._generation = 0
        shared_delivery().delivered.connect(self._on_delivered)
        self._build()

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        self._status = _label(self, "gpu-offers-status", UNCHECKED_TEXT)
        layout.addWidget(self._status)

        filters = QHBoxLayout()
        self._name = QLineEdit(self)
        self._name.setObjectName("gpu-offers-name")
        self._name.setPlaceholderText("GPU name or maker")
        filters.addWidget(self._name)

        self._memory = QSpinBox(self)
        self._memory.setObjectName("gpu-offers-memory")
        self._memory.setRange(0, 1024)
        self._memory.setSpecialValueText("Any memory")
        self._memory.setSuffix(" GB or more")
        filters.addWidget(self._memory)

        self._tier = QComboBox(self)
        self._tier.setObjectName("gpu-offers-tier")
        for text, value in TIER_CHOICES:
            self._tier.addItem(text, value)
        filters.addWidget(self._tier)

        self._center = QComboBox(self)
        self._center.setObjectName("gpu-offers-center")
        self._center.addItem("Any data center", "")
        filters.addWidget(self._center)

        self._maker = QComboBox(self)
        self._maker.setObjectName("gpu-offers-maker")
        self._maker.addItem(ANY_MAKER_TEXT, "")
        filters.addWidget(self._maker)

        self._in_stock = QCheckBox(IN_STOCK_TEXT, self)
        self._in_stock.setObjectName("gpu-offers-in-stock")
        self._in_stock.setChecked(True)
        filters.addWidget(self._in_stock)

        self._sort = QComboBox(self)
        self._sort.setObjectName("gpu-offers-sort")
        for text, key in SORT_CHOICES:
            self._sort.addItem(text, key)
        filters.addWidget(self._sort)

        self._max_price = QDoubleSpinBox(self)
        self._max_price.setObjectName("gpu-offers-price")
        self._max_price.setRange(0.0, 1000.0)
        self._max_price.setDecimals(2)
        self._max_price.setSingleStep(0.25)
        self._max_price.setSpecialValueText("Any price")
        self._max_price.setPrefix("max $")
        self._max_price.setSuffix(" per hour")
        filters.addWidget(self._max_price)
        layout.addLayout(filters)

        self._check = QPushButton("Check again", self)
        self._check.setObjectName("gpu-offers-check")
        layout.addWidget(self._check)

        self._table = QTableWidget(0, len(COLUMNS), self)
        self._table.setObjectName("gpu-offers-table")
        self._table.setHorizontalHeaderLabels(list(COLUMNS))
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self._table, 1)

        self._name.textChanged.connect(self._apply_filters)
        self._memory.valueChanged.connect(self._apply_filters)
        self._tier.currentIndexChanged.connect(self._apply_filters)
        self._center.currentIndexChanged.connect(self._apply_filters)
        self._maker.currentIndexChanged.connect(self._apply_filters)
        self._in_stock.toggled.connect(self._apply_filters)
        self._sort.currentIndexChanged.connect(self._apply_filters)
        self._max_price.valueChanged.connect(self._apply_filters)
        self._check.clicked.connect(self._on_check_clicked)

    # ----------------------------------------------------------- checks

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        if not self._probed_on_show:
            self._probed_on_show = True
            self._start_check()

    @Slot()
    def _on_check_clicked(self) -> None:
        self._start_check()

    def _start_check(self) -> None:
        if self._checking:
            return
        self._checking = True
        self._generation += 1
        generation = self._generation
        self._check.setEnabled(False)
        self._status.setText(CHECKING_TEXT)
        # The job holds the key reader, the fetch function, this panel's owner token and the shared delivery object.
        # It never holds the panel, so a check still running when the panel is deleted cannot keep it alive.
        key_reader = self._key_reader
        fetch = self._fetch
        owner = self._owner
        delivery = shared_delivery()

        def read_and_fetch() -> tuple[GpuOffer, ...]:
            return fetch(key_reader())

        def deliver(payload: tuple) -> None:
            delivery.delivered.emit(Delivered(owner, (generation, payload)))

        # The job reads the key on the worker thread, so the key never touches the UI thread.
        QThreadPool.globalInstance().start(_CheckJob(read_and_fetch, deliver))

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another panel, or for an older check, is dropped here.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        generation, payload = item.value
        if generation != self._generation:
            return
        self._on_check_finished(payload)

    def _on_check_finished(self, payload: tuple) -> None:
        offers, failure = payload
        self._checking = False
        self._check.setEnabled(True)
        if failure is not None or not isinstance(offers, tuple):
            self._offers = ()
            self._table.setRowCount(0)
            self._status.setText(failure or FAILED_TEXT.format("NoResult"))
            return
        self._offers = offers
        self._checked_text = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        self._refresh_data_centers()
        self._refresh_makers()
        self._apply_filters()

    def _refresh_data_centers(self) -> None:
        """Rebuild the data-center choices from the offers, and keep the choice made before if it still exists."""
        chosen = self._center.currentData() or ""
        self._center.blockSignals(True)
        self._center.clear()
        self._center.addItem("Any data center", "")
        for center_id in data_center_ids(self._offers):
            self._center.addItem(center_id, center_id)
        index = self._center.findData(chosen)
        self._center.setCurrentIndex(index if index >= 0 else 0)
        self._center.blockSignals(False)

    def _refresh_makers(self) -> None:
        """Rebuild the maker choices from the offers, and keep the choice made before if it still exists."""
        chosen = self._maker.currentData() or ""
        makers = sorted({offer.manufacturer for offer in self._offers if offer.manufacturer})
        self._maker.blockSignals(True)
        self._maker.clear()
        self._maker.addItem(ANY_MAKER_TEXT, "")
        for maker in makers:
            self._maker.addItem(maker, maker)
        index = self._maker.findData(chosen)
        self._maker.setCurrentIndex(index if index >= 0 else 0)
        self._maker.blockSignals(False)

    # ----------------------------------------------------------- filters

    def _apply_filters(self, *_args: Any) -> None:
        kept = filter_offers(
            self._offers,
            text=self._name.text(),
            min_memory_gb=self._memory.value(),
            tier=self._tier.currentData() or TIER_ANY,
            data_center=self._center.currentData() or "",
            max_price=self._max_price.value(),
            manufacturer=self._maker.currentData() or "",
            in_stock_only=self._in_stock.isChecked(),
        )
        kept = sort_offers(kept, self._sort.currentData() or "name")
        self._table.setRowCount(len(kept))
        for row, offer in enumerate(kept):
            for column, value in enumerate(offer_cells(offer)):
                cell = QTableWidgetItem(value)
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(row, column, cell)
        if self._offers:
            self._status.setText(
                f"Showing {len(kept)} of {len(self._offers)} GPU types. Checked at {self._checked_text}."
            )


__all__ = [
    "COLUMNS",
    "GpuOffersPanel",
    "centers_text",
    "counts_text",
    "data_center_ids",
    "money_text",
    "offer_cells",
]
