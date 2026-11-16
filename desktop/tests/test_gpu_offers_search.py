"""RunPod GPU offers: search that ignores case, spaces and hyphens, the maker filter, the in-stock filter, and sorting.

Pure tests need no Qt. The panel tests use the fake catalog from test_gpu_offers.py, so no network opens.
"""

import pytest
from PySide6.QtWidgets import QCheckBox, QComboBox, QTableWidget

from meridian_desktop.gpu_catalog import DataCenterStock, GpuOffer, filter_offers, in_stock, search_matches, sort_offers

from test_gpu_offers import A100, L4, MI300X, OFFERS, _FakeCatalog
from test_gpu_offers import _shown as shown_panel


def _offer(gpu_id, name, maker="NVIDIA", memory=24, availability="LOW", centers=()):
    return GpuOffer(
        gpu_id=gpu_id,
        name=name,
        manufacturer=maker,
        memory_gb=memory,
        availability=availability,
        price_secure=None,
        price_community=None,
        max_count_secure=None,
        max_count_community=None,
        data_centers=centers,
    )


SXM = _offer("NVIDIA A100-SXM4-80GB", "A100 SXM 80GB", memory=80)
PCIE_H100 = _offer("NVIDIA H100 PCIe", "H100 PCIe", memory=80)
RTX_4090 = _offer("NVIDIA GeForce RTX 4090", "RTX 4090", memory=24, availability="NONE")


# ----------------------------------------------------------- search, without Qt


@pytest.mark.parametrize(
    "query, offer, expected",
    [
        ("a100 sxm", SXM, True),
        ("A100SXM", SXM, True),
        ("a100-sxm4", SXM, True),
        ("rtx4090", RTX_4090, True),
        ("rtx 4090", RTX_4090, True),
        ("h100 pcie", PCIE_H100, True),
        ("h100 sxm", PCIE_H100, False),
        ("", SXM, True),
        ("   ", SXM, True),
    ],
)
def test_search_ignores_case_spaces_and_hyphens(query, offer, expected):
    assert search_matches(query, offer) is expected


def test_search_needs_every_word_to_match():
    assert filter_offers(OFFERS, text="nvidia l4") == (L4,)
    assert filter_offers(OFFERS, text="nvidia mi300") == ()


# ----------------------------------------------------------- stock and maker, without Qt


def test_in_stock_is_a_level_above_none_overall_and_per_data_center():
    assert in_stock(A100) is True
    assert in_stock(MI300X) is False  # RunPod reports NONE
    assert in_stock(L4, "US-KS-2") is True
    assert in_stock(L4, "NO-SUCH-CENTER") is False


def test_in_stock_only_keeps_gpus_with_a_stock_level_above_none():
    assert filter_offers(OFFERS, in_stock_only=True) == (A100, L4)
    assert filter_offers(OFFERS, in_stock_only=True, data_center="US-KS-2") == (L4,)


def test_the_maker_filter_ignores_case_and_keeps_one_maker():
    assert filter_offers(OFFERS, manufacturer="amd") == (MI300X,)
    assert filter_offers(OFFERS, manufacturer="NVIDIA") == (A100, L4)
    assert filter_offers(OFFERS, manufacturer="") == OFFERS


# ----------------------------------------------------------- sorting, without Qt


def test_sorting_by_name_memory_price_and_stock():
    assert [o.name for o in sort_offers(OFFERS, "name")] == ["A100 PCIe", "L4", "MI300X"]
    assert [o.name for o in sort_offers(OFFERS, "memory_desc")] == ["MI300X", "A100 PCIe", "L4"]
    assert [o.name for o in sort_offers(OFFERS, "memory_asc")] == ["L4", "A100 PCIe", "MI300X"]
    # Secure price: MI300X is offered in secure at 2.39 and L4 is not offered there, so L4 sorts last.
    assert [o.name for o in sort_offers(OFFERS, "secure_price")] == ["A100 PCIe", "MI300X", "L4"]
    # Community price: MI300X is not offered in community, so it sorts last.
    assert [o.name for o in sort_offers(OFFERS, "community_price")] == ["L4", "A100 PCIe", "MI300X"]
    # Stock: MEDIUM (L4) first, then LOW (A100), then NONE (MI300X).
    assert [o.name for o in sort_offers(OFFERS, "stock")] == ["L4", "A100 PCIe", "MI300X"]


def test_an_unknown_sort_key_is_refused():
    with pytest.raises(ValueError):
        sort_offers(OFFERS, "performance-guess")


# ----------------------------------------------------------- the panel


def test_in_stock_is_on_by_default_and_hides_gpus_with_no_stock(qtbot):
    panel = shown_panel(qtbot, _FakeCatalog())
    table = panel.findChild(QTableWidget, "gpu-offers-table")
    assert panel.findChild(QCheckBox, "gpu-offers-in-stock").isChecked() is True
    assert table.rowCount() == 2  # MI300X has no stock, so it is hidden
    panel.findChild(QCheckBox, "gpu-offers-in-stock").setChecked(False)
    assert table.rowCount() == 3


def test_the_maker_picker_lists_each_maker_and_narrows_the_table(qtbot):
    panel = shown_panel(qtbot, _FakeCatalog())
    panel.findChild(QCheckBox, "gpu-offers-in-stock").setChecked(False)
    maker = panel.findChild(QComboBox, "gpu-offers-maker")
    assert [maker.itemText(i) for i in range(maker.count())] == ["Any maker", "AMD", "NVIDIA"]
    maker.setCurrentIndex(maker.findData("AMD"))
    table = panel.findChild(QTableWidget, "gpu-offers-table")
    assert table.rowCount() == 1
    assert table.item(0, 0).text() == "MI300X"


def test_the_sort_picker_orders_the_rows(qtbot):
    panel = shown_panel(qtbot, _FakeCatalog())
    panel.findChild(QCheckBox, "gpu-offers-in-stock").setChecked(False)
    sort = panel.findChild(QComboBox, "gpu-offers-sort")
    sort.setCurrentIndex(sort.findData("memory_asc"))
    table = panel.findChild(QTableWidget, "gpu-offers-table")
    assert [table.item(row, 0).text() for row in range(table.rowCount())] == ["L4", "A100 PCIe", "MI300X"]
