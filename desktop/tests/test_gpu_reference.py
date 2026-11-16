"""Vendor datasheet reference figures: the lookups, the shape of the table, the sort orders, and the panel columns.

The panel test uses the fake catalog from test_gpu_offers.py, so no network opens.
"""

from PySide6.QtWidgets import QCheckBox, QLabel, QTableWidget

from meridian_desktop.gpu_catalog import GpuOffer, sort_offers
from meridian_desktop.gpu_reference import SPECS, spec_for
from meridian_desktop.ui.gpu_offers_panel import (
    COLUMNS,
    NOT_IN_REFERENCE_TEXT,
    NOT_STATED_TEXT,
    REFERENCE_NOTE_TEXT,
    offer_cells,
)

from test_gpu_offers import _FakeCatalog, _shown


def _offer(gpu_id):
    return GpuOffer(
        gpu_id=gpu_id,
        name=gpu_id,
        manufacturer="NVIDIA",
        memory_gb=24,
        availability="LOW",
        price_secure=None,
        price_community=None,
        max_count_secure=None,
        max_count_community=None,
        data_centers=(),
    )


def test_known_models_have_the_vendor_figures():
    a100 = spec_for("NVIDIA A100 80GB PCIe")
    assert (a100.fp16_tensor_tflops_dense, a100.memory_bandwidth_gbs) == (312, 1935)
    assert spec_for("NVIDIA GeForce RTX 4090").fp16_tensor_tflops_dense == 330.3
    assert spec_for("NVIDIA B200").fp16_tensor_tflops_dense is None
    assert spec_for("a model that is not listed") is None


def test_every_entry_has_positive_or_no_figures_an_https_source_and_a_note():
    for gpu_id, spec in SPECS.items():
        for value in (spec.fp16_tensor_tflops_dense, spec.memory_bandwidth_gbs):
            assert value is None or value > 0, gpu_id
        assert spec.source_url.startswith("https://"), gpu_id
        assert spec.note.strip(), gpu_id


def test_sorting_by_tflops_puts_the_models_without_a_figure_last():
    offers = [_offer("NVIDIA L4"), _offer("NVIDIA B200"), _offer("NVIDIA H100 PCIe"), _offer("NVIDIA NOT LISTED")]
    ordered = [offer.gpu_id for offer in sort_offers(offers, "tflops_desc")]
    assert ordered == ["NVIDIA H100 PCIe", "NVIDIA L4", "NVIDIA B200", "NVIDIA NOT LISTED"]


def test_sorting_by_bandwidth_puts_the_models_without_a_figure_last():
    offers = [_offer("NVIDIA L4"), _offer("NVIDIA H200"), _offer("NVIDIA H200 NVL"), _offer("NVIDIA B200")]
    ordered = [offer.gpu_id for offer in sort_offers(offers, "bandwidth_desc")]
    assert ordered == ["NVIDIA H200", "NVIDIA H200 NVL", "NVIDIA L4", "NVIDIA B200"]


def test_a_cell_says_not_stated_or_not_in_reference_rather_than_guessing():
    assert offer_cells(_offer("NVIDIA B200"))[8] == NOT_STATED_TEXT
    assert offer_cells(_offer("NVIDIA B200"))[9] == NOT_STATED_TEXT
    assert offer_cells(_offer("NVIDIA NOT LISTED"))[8] == NOT_IN_REFERENCE_TEXT


def test_the_panel_shows_the_vendor_figures_and_the_caveat(qtbot):
    panel = _shown(qtbot, _FakeCatalog())
    panel.findChild(QCheckBox, "gpu-offers-in-stock").setChecked(False)
    table = panel.findChild(QTableWidget, "gpu-offers-table")
    assert table.columnCount() == len(COLUMNS) == 10
    assert table.horizontalHeaderItem(8).text() == "Peak FP16 TFLOPS (dense)"
    assert table.horizontalHeaderItem(9).text() == "Bandwidth GB/s"
    rows = {table.item(row, 0).text(): row for row in range(table.rowCount())}
    assert table.item(rows["A100 PCIe"], 8).text() == "312"
    assert table.item(rows["A100 PCIe"], 9).text() == "1935"
    assert table.item(rows["MI300X"], 8).text() == "1307.4"
    assert table.item(rows["L4"], 9).text() == "300"
    assert panel.findChild(QLabel, "gpu-offers-reference-note").text() == REFERENCE_NOTE_TEXT
