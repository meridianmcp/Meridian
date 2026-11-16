"""RunPod GPU offers: the filters, the cell text, and the panel. No network: the panel gets a fake catalog.

The offers are built from the same fields RunPod's v2 catalog returned, with made-up key values.
"""

from PySide6.QtWidgets import QCheckBox, QComboBox, QLabel, QLineEdit, QPushButton, QTableWidget

from meridian_desktop.gpu_catalog import (
    NO_KEY_MESSAGE,
    TIER_ANY,
    TIER_COMMUNITY,
    TIER_SECURE,
    DataCenterStock,
    GpuCatalogError,
    GpuOffer,
    filter_offers,
    tier_price,
)
from meridian_desktop.ui.gpu_offers_panel import (
    CHECKING_TEXT,
    COLUMNS,
    FAILED_TEXT,
    GpuOffersPanel,
    centers_text,
    counts_text,
    data_center_ids,
    money_text,
    offer_cells,
)

A100 = GpuOffer(
    gpu_id="NVIDIA A100 80GB PCIe",
    name="A100 PCIe",
    manufacturer="NVIDIA",
    memory_gb=80,
    availability="LOW",
    price_secure=2.09,
    price_community=1.19,
    max_count_secure=8,
    max_count_community=6,
    data_centers=(DataCenterStock(id="CA-MTL-3", availability="LOW"),),
)
MI300X = GpuOffer(
    gpu_id="AMD Instinct MI300X OAM",
    name="MI300X",
    manufacturer="AMD",
    memory_gb=192,
    availability="NONE",
    price_secure=2.39,
    price_community=0.5,
    max_count_secure=8,
    max_count_community=0,
    data_centers=(),
)
L4 = GpuOffer(
    gpu_id="NVIDIA L4",
    name="L4",
    manufacturer="NVIDIA",
    memory_gb=24,
    availability="MEDIUM",
    price_secure=0.69,
    price_community=0.44,
    max_count_secure=0,
    max_count_community=8,
    data_centers=(DataCenterStock(id="US-KS-2", availability="MEDIUM"), DataCenterStock(id="CA-MTL-3", availability="HIGH")),
)
OFFERS = (A100, MI300X, L4)


# ----------------------------------------------------------- filters, without Qt


def test_text_matches_the_id_name_or_maker_without_case():
    assert filter_offers(OFFERS, text="mi300") == (MI300X,)
    assert filter_offers(OFFERS, text="NVIDIA") == (A100, L4)
    assert filter_offers(OFFERS, text="  ") == OFFERS


def test_minimum_memory_keeps_only_gpus_at_or_above_it():
    assert filter_offers(OFFERS, min_memory_gb=48) == (A100, MI300X)
    assert filter_offers(OFFERS, min_memory_gb=0) == OFFERS


def test_secure_tier_drops_gpus_capped_at_zero_in_secure_cloud():
    assert filter_offers(OFFERS, tier=TIER_SECURE) == (A100, MI300X)


def test_community_tier_drops_gpus_capped_at_zero_in_community_cloud():
    assert filter_offers(OFFERS, tier=TIER_COMMUNITY) == (A100, L4)


def test_data_center_keeps_only_gpus_listed_in_it():
    assert filter_offers(OFFERS, data_center="CA-MTL-3") == (A100, L4)
    assert filter_offers(OFFERS, data_center="US-KS-2") == (L4,)


def test_max_price_uses_the_lowest_offered_tier_for_any_cloud():
    # A100's lowest offered price is 1.19 and MI300X's only offered tier is secure at 2.39, so only L4 is kept.
    assert filter_offers(OFFERS, max_price=1.0) == (L4,)


def test_max_price_on_one_tier_drops_gpus_the_tier_does_not_offer():
    assert filter_offers(OFFERS, tier=TIER_SECURE, max_price=1.0) == ()


def test_tier_price_is_the_lowest_offered_price_or_none():
    assert tier_price(L4, TIER_ANY) == 0.44
    assert tier_price(L4, TIER_SECURE) is None
    assert tier_price(MI300X, TIER_COMMUNITY) is None


def test_cell_text_for_money_counts_centers_and_data_center_ids():
    assert money_text(None) == "not offered"
    assert money_text(2.09) == "$2.09"
    assert counts_text(8, 0) == "secure up to 8"
    assert counts_text(0, 0) == "none"
    assert counts_text(8, 6) == "secure up to 8, community up to 6"
    assert centers_text(()) == "none listed"
    assert centers_text((DataCenterStock(id="CA-MTL-3", availability="LOW"),)) == "CA-MTL-3 (low)"
    assert data_center_ids(OFFERS) == ["CA-MTL-3", "US-KS-2"]
    assert offer_cells(MI300X)[4] == "$2.39"
    assert offer_cells(L4)[4] == "not offered"
    assert len(offer_cells(A100)) == len(COLUMNS)


# ----------------------------------------------------------- the panel, with a fake catalog


class _FakeCatalog:
    """Stands in for the catalog read. It records each key it was given, and returns offers or raises."""

    def __init__(self, offers=OFFERS, failure=None):
        self.offers = offers
        self.failure = failure
        self.keys = []

    def __call__(self, key):
        self.keys.append(key)
        if self.failure is not None:
            raise self.failure
        return self.offers


def _shown(qtbot, catalog):
    panel = GpuOffersPanel(key_reader=lambda: "KEY", fetch=catalog)
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitExposed(panel)
    check = panel.findChild(QPushButton, "gpu-offers-check")
    status = panel.findChild(QLabel, "gpu-offers-status")
    qtbot.waitUntil(lambda: check.isEnabled() and status.text() != CHECKING_TEXT, timeout=5000)
    return panel


def test_the_panel_lists_each_gpu_with_its_stock_prices_and_data_centers(qtbot):
    panel = _shown(qtbot, _FakeCatalog())
    panel.findChild(QCheckBox, "gpu-offers-in-stock").setChecked(False)  # show every GPU, not only the ones in stock
    table = panel.findChild(QTableWidget, "gpu-offers-table")
    assert table.rowCount() == 3
    first = [table.item(0, column).text() for column in range(len(COLUMNS))]
    assert first[0] == "A100 PCIe"
    assert first[3] == "LOW"
    assert first[4] == "$2.09"
    assert first[7] == "CA-MTL-3 (low)"


def test_the_key_is_read_once_per_check_and_passed_to_the_catalog(qtbot):
    catalog = _FakeCatalog()
    _shown(qtbot, catalog)
    assert catalog.keys == ["KEY"]


def test_no_saved_key_shows_the_fixed_message_and_lists_nothing(qtbot):
    panel = _shown(qtbot, _FakeCatalog(failure=GpuCatalogError(NO_KEY_MESSAGE)))
    assert panel.findChild(QLabel, "gpu-offers-status").text() == NO_KEY_MESSAGE
    assert panel.findChild(QTableWidget, "gpu-offers-table").rowCount() == 0


def test_a_failed_check_shows_only_the_error_class_name(qtbot):
    panel = _shown(qtbot, _FakeCatalog(failure=ValueError("boom with a secret value")))
    status = panel.findChild(QLabel, "gpu-offers-status").text()
    assert status == FAILED_TEXT.format("ValueError")
    assert "secret" not in status


def test_the_name_filter_narrows_the_table_and_the_status_counts_it(qtbot):
    panel = _shown(qtbot, _FakeCatalog())
    panel.findChild(QLineEdit, "gpu-offers-name").setText("L4")
    table = panel.findChild(QTableWidget, "gpu-offers-table")
    assert table.rowCount() == 1
    assert "Showing 1 of 3 GPU types" in panel.findChild(QLabel, "gpu-offers-status").text()


def test_check_again_reads_the_catalog_once_more(qtbot):
    catalog = _FakeCatalog()
    panel = _shown(qtbot, catalog)
    check = panel.findChild(QPushButton, "gpu-offers-check")
    check.click()
    qtbot.waitUntil(lambda: len(catalog.keys) == 2 and check.isEnabled(), timeout=5000)
    assert catalog.keys == ["KEY", "KEY"]
