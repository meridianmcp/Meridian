"""Tests for meridian.compute_catalog: GPU normalisation, machine totals, filters, sorts, grouping, the query
parser and the response view.

Pure logic: no network, no provider SDK, no clock except where a test injects one. Offers are built in place.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from meridian import compute_catalog as cc
from meridian import compute_options as co

SENTINEL = "SENTINEL-QUERY-VALUE-DO-NOT-ECHO-4c1e"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Refuse DNS and outbound connections for every test in this module."""
    import socket

    def _refuse(*_args, **_kwargs):
        raise RuntimeError("network access is disabled in this test")

    monkeypatch.setattr(socket, "getaddrinfo", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)


def _row(**kw) -> co.Offer:
    base = dict(
        id="runpod:gpu:secure:-",
        provider="runpod",
        vram_gb=None,
        ram_gb=None,
        vcpu=None,
        gpu="gpu",
        available=True,
        price_as_of="2026-11-09T14:03:00Z",
        gpu_model="h100-80gb-sxm",
        gpu_variant="sxm",
        gpu_count=1,
        max_gpu_count=8,
        price_per_gpu_hour=2.0,
        vram_gb_per_gpu=80,
        cloud_type="secure",
        data_center=None,
        stock_status="HIGH",
    )
    base.update(kw)
    # A base row (one GPU) has its machine price equal to its per-GPU price.
    base.setdefault("price_per_hour", base["price_per_gpu_hour"])
    return co.Offer(**base)


# --- normalize_gpu --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "memory", "expected"),
    [
        ("NVIDIA H100 SXM", 80, ("h100-80gb-sxm", "sxm")),
        ("NVIDIA H100 PCIe", 80, ("h100-80gb-pcie", "pcie")),
        ("H100 80GB HBM3", None, ("h100-80gb", None)),
        ("NVIDIA GeForce RTX 4090", 24, ("rtx-4090-24gb", None)),
        ("A100-SXM4-80GB", None, ("a100-80gb-sxm", "sxm")),
        ("NVIDIA RTX A6000", 48, ("rtx-a6000-48gb", None)),
        ("L40S", 48, ("l40s-48gb", None)),
        ("NVIDIA H200 NVL", 141, ("h200-141gb-nvl", "nvl")),
        ("NVIDIA Tesla T4", 16, ("t4-16gb", None)),
    ],
)
def test_normalize_gpu_builds_a_family_memory_variant_key(name, memory, expected):
    assert cc.normalize_gpu(name, memory) == expected


def test_sxm_and_pcie_are_never_merged_into_one_model():
    sxm, _ = cc.normalize_gpu("NVIDIA H100 SXM", 80)
    pcie, _ = cc.normalize_gpu("NVIDIA H100 PCIe", 80)
    assert sxm != pcie


def test_a_blank_or_missing_name_gives_no_model():
    assert cc.normalize_gpu(None, 80) == (None, None)
    assert cc.normalize_gpu("   ", 80) == (None, None)
    assert cc.normalize_gpu("NVIDIA", 80) == (None, None)


def test_invalid_memory_falls_back_to_a_token_in_the_name():
    assert cc.normalize_gpu("H100 80GB", 0) == ("h100-80gb", None)
    assert cc.normalize_gpu("H100 80GB", True) == ("h100-80gb", None)
    assert cc.normalize_gpu("H100 80GB", "80") == ("h100-80gb", None)
    assert cc.normalize_gpu("H100", 80.0) == ("h100-80gb", None)


def test_unknown_memory_keeps_a_family_only_model_that_does_not_merge_with_sized_rows():
    family_only, _ = cc.normalize_gpu("Mystery Accelerator", None)
    sized, _ = cc.normalize_gpu("Mystery Accelerator", 40)
    assert family_only == "mystery-accelerator"
    assert sized == "mystery-accelerator-40gb"
    assert family_only != sized


def test_alias_table_maps_a_normalised_model_when_an_entry_exists(monkeypatch):
    monkeypatch.setitem(cc.GPU_ALIASES, "h100-80gb-sxm", "hopper-h100-80gb-sxm")
    assert cc.normalize_gpu("NVIDIA H100 SXM", 80) == ("hopper-h100-80gb-sxm", "sxm")


def test_the_alias_table_is_empty_until_a_second_provider_lands():
    assert cc.GPU_ALIASES == {}


# --- for_gpu_count --------------------------------------------------------------------------------


def test_for_gpu_count_multiplies_price_and_vram_and_leaves_the_base_row_alone():
    base = _row(price_per_gpu_hour=2.0, vram_gb_per_gpu=80, gpu_count=1)
    scaled = cc.for_gpu_count(base, 4)
    assert scaled.price_per_hour == pytest.approx(8.0)
    assert scaled.vram_gb == pytest.approx(320.0)
    assert scaled.gpu_count == 4
    assert scaled.price_per_gpu_hour == 2.0
    assert base.price_per_hour == 2.0 and base.gpu_count == 1


def test_for_gpu_count_with_an_unknown_per_gpu_price_has_an_unknown_machine_price():
    scaled = cc.for_gpu_count(_row(price_per_gpu_hour=None, vram_gb_per_gpu=None), 2)
    assert scaled.price_per_hour is None
    assert scaled.vram_gb is None


def test_for_gpu_count_accepts_a_legacy_row_with_no_count_set():
    legacy = co.Offer(id="x", provider="runpod", price_per_hour=1.0, price_per_gpu_hour=1.0, vram_gb_per_gpu=24)
    assert cc.for_gpu_count(legacy, 2).gpu_count == 2


@pytest.mark.parametrize("count", [0, -1, True, 2.0])
def test_for_gpu_count_rejects_a_bad_count(count):
    with pytest.raises(ValueError):
        cc.for_gpu_count(_row(), count)


def test_for_gpu_count_refuses_a_row_that_is_already_scaled():
    with pytest.raises(ValueError):
        cc.for_gpu_count(_row(gpu_count=4), 2)


# --- filter_offers --------------------------------------------------------------------------------


def test_filters_match_model_cloud_stock_price_and_count():
    rows = [
        _row(id="a", gpu_model="h100-80gb-sxm", cloud_type="secure", stock_status="HIGH", price_per_gpu_hour=2.0),
        _row(id="b", gpu_model="h100-80gb-sxm", cloud_type="community", stock_status="NONE", price_per_gpu_hour=1.5),
        _row(id="c", gpu_model="l40s-48gb", cloud_type="secure", stock_status=None, price_per_gpu_hour=None),
        _row(id="d", gpu_model="l40s-48gb", cloud_type="secure", stock_status="LOW", max_gpu_count=2),
    ]
    assert [o.id for o in cc.filter_offers(rows, gpu_model="l40s-48gb")] == ["c", "d"]
    assert [o.id for o in cc.filter_offers(rows, cloud_type="community")] == ["b"]
    assert [o.id for o in cc.filter_offers(rows, in_stock_only=True)] == ["a", "d"]
    assert [o.id for o in cc.filter_offers(rows, max_price_per_gpu_hour=1.8)] == ["b"]
    assert [o.id for o in cc.filter_offers(rows, gpu_count=4)] == ["a", "b", "c"]


def test_unknown_stock_is_excluded_by_in_stock_only_and_unknown_price_by_a_cap():
    unknown_stock = _row(id="u", stock_status=None)
    unknown_price = _row(id="p", price_per_gpu_hour=None)
    assert cc.filter_offers([unknown_stock], in_stock_only=True) == []
    assert cc.filter_offers([unknown_price], max_price_per_gpu_hour=10.0) == []


def test_an_unknown_ceiling_is_kept_when_a_count_is_requested():
    assert cc.filter_offers([_row(max_gpu_count=None)], gpu_count=8) != []


# --- sort_offers ----------------------------------------------------------------------------------


def test_sort_by_price_ascending_puts_unknown_last():
    rows = [
        _row(id="x", price_per_gpu_hour=3.0),
        _row(id="none", price_per_gpu_hour=None),
        _row(id="y", price_per_gpu_hour=1.0),
    ]
    assert [o.id for o in cc.sort_offers(rows, "price_per_gpu_hour", "asc")] == ["y", "x", "none"]


def test_sort_by_price_descending_still_puts_unknown_last():
    rows = [
        _row(id="x", price_per_gpu_hour=3.0),
        _row(id="none", price_per_gpu_hour=None),
        _row(id="y", price_per_gpu_hour=1.0),
    ]
    assert [o.id for o in cc.sort_offers(rows, "price_per_gpu_hour", "desc")] == ["x", "y", "none"]


def test_ties_break_on_provider_model_cloud_then_data_center_in_both_directions():
    rows = [
        _row(id="c", gpu_model="b-model", cloud_type="secure", data_center="z", price_per_gpu_hour=1.0),
        _row(id="a", gpu_model="a-model", cloud_type="secure", data_center="z", price_per_gpu_hour=1.0),
        _row(id="b", gpu_model="a-model", cloud_type="community", data_center="y", price_per_gpu_hour=1.0),
        _row(id="d", gpu_model="a-model", cloud_type="community", data_center="a", price_per_gpu_hour=1.0),
    ]
    asc = [o.id for o in cc.sort_offers(rows, "price_per_gpu_hour", "asc")]
    desc = [o.id for o in cc.sort_offers(rows, "price_per_gpu_hour", "desc")]
    assert asc == ["d", "b", "a", "c"]
    assert desc == asc


def test_stock_sort_ranks_none_low_medium_high_and_puts_unknown_last():
    rows = [
        _row(id="h", stock_status="HIGH"),
        _row(id="n", stock_status="NONE"),
        _row(id="u", stock_status=None),
        _row(id="m", stock_status="MEDIUM"),
    ]
    assert [o.id for o in cc.sort_offers(rows, "stock", "desc")] == ["h", "m", "n", "u"]
    assert [o.id for o in cc.sort_offers(rows, "stock", "asc")] == ["n", "m", "h", "u"]


@pytest.mark.parametrize(("key", "order"), [("bogus", "asc"), ("price_per_gpu_hour", "sideways")])
def test_sort_rejects_an_unknown_key_or_order(key, order):
    with pytest.raises(ValueError):
        cc.sort_offers([_row()], key, order)


def test_machine_price_sort_uses_the_scaled_rows():
    small = cc.for_gpu_count(_row(id="s", price_per_gpu_hour=1.0), 2)
    big = cc.for_gpu_count(_row(id="b", price_per_gpu_hour=1.5), 2)
    assert [o.id for o in cc.sort_offers([big, small], "price_per_hour", "asc")] == ["s", "b"]


# --- grouping and the model summary ---------------------------------------------------------------


def test_group_by_model_and_summary_report_count_and_the_lowest_known_price():
    rows = [
        _row(id="a", gpu_model="l40s-48gb", price_per_gpu_hour=0.9),
        _row(id="b", gpu_model="l40s-48gb", price_per_gpu_hour=None),
        _row(id="c", gpu_model="a100-80gb-sxm", price_per_gpu_hour=1.4),
        _row(id="d", gpu_model="a100-80gb-sxm", price_per_gpu_hour=1.1),
    ]
    groups = cc.group_by_gpu_model(rows)
    assert sorted(groups) == ["a100-80gb-sxm", "l40s-48gb"]
    assert [o.id for o in groups["l40s-48gb"]] == ["a", "b"]
    summary = cc.summarize_gpu_models(rows)
    assert summary == [
        {"gpu_model": "a100-80gb-sxm", "count": 2, "min_price_per_gpu_hour": 1.1},
        {"gpu_model": "l40s-48gb", "count": 2, "min_price_per_gpu_hour": 0.9},
    ]


def test_a_model_with_no_known_price_has_a_none_minimum():
    summary = cc.summarize_gpu_models([_row(gpu_model="x", price_per_gpu_hour=None)])
    assert summary == [{"gpu_model": "x", "count": 1, "min_price_per_gpu_hour": None}]


# --- format_utc_stamp -----------------------------------------------------------------------------


def test_format_utc_stamp_writes_iso_utc_and_converts_other_offsets():
    assert cc.format_utc_stamp(datetime(2026, 11, 9, 14, 3, tzinfo=timezone.utc)) == "2026-11-09T14:03:00Z"
    offset = timezone(timedelta(hours=2))
    assert cc.format_utc_stamp(datetime(2026, 11, 9, 16, 3, tzinfo=offset)) == "2026-11-09T14:03:00Z"
    assert cc.format_utc_stamp(None) is None


def test_format_utc_stamp_refuses_a_naive_time():
    with pytest.raises(ValueError):
        cc.format_utc_stamp(datetime(2026, 11, 9, 14, 3))


# --- parse_offers_query ---------------------------------------------------------------------------


def _q(**params):
    return {name: value if isinstance(value, list) else [value] for name, value in params.items()}


def test_an_empty_query_gives_the_documented_defaults():
    assert cc.parse_offers_query({}) == cc.OffersQuery()


def test_a_valid_query_is_parsed_into_typed_fields():
    query = cc.parse_offers_query(
        _q(
            provider="runpod",
            gpu_model="H100-80GB-SXM",
            cloud_type="spot",
            gpu_count="4",
            in_stock_only="true",
            max_price_per_gpu_hour="2.5",
            sort="stock",
            order="desc",
        )
    )
    assert query.gpu_model == "h100-80gb-sxm"
    assert query.cloud_type == "spot"
    assert query.gpu_count == 4
    assert query.in_stock_only is True
    assert query.max_price_per_gpu_hour == 2.5
    assert (query.sort, query.order) == ("stock", "desc")


@pytest.mark.parametrize("name", ["api_key", "APIKEY", "gpu_model2", "foo"])
def test_an_unknown_parameter_is_refused_with_400_code_and_does_not_echo_the_value(name):
    with pytest.raises(cc.QueryError) as info:
        cc.parse_offers_query({name: [SENTINEL]})
    assert info.value.code == "unknown_parameter"
    assert SENTINEL not in info.value.message


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("provider", "aws"),
        ("gpu_model", "has space"),
        ("gpu_model", "x" * 65),
        ("cloud_type", "bogus"),
        ("gpu_count", "0"),
        ("gpu_count", "9"),
        ("gpu_count", "two"),
        ("in_stock_only", "maybe"),
        ("max_price_per_gpu_hour", "0"),
        ("max_price_per_gpu_hour", "-1"),
        ("max_price_per_gpu_hour", "1e3"),
        ("max_price_per_gpu_hour", "cheap"),
        ("sort", "bogus"),
        ("order", "sideways"),
    ],
)
def test_an_invalid_value_is_refused_with_invalid_filter_and_never_echoed(name, value):
    # The sentinel rides inside the supplied value, so a message that echoed it would fail the assertion.
    supplied = {name: [f"{value} {SENTINEL}"]}
    with pytest.raises(cc.QueryError) as info:
        cc.parse_offers_query(supplied)
    assert info.value.code == "invalid_filter"
    assert SENTINEL not in info.value.message


def test_a_repeated_parameter_is_refused_with_invalid_filter():
    with pytest.raises(cc.QueryError) as info:
        cc.parse_offers_query({"gpu_count": ["1", "2"]})
    assert info.value.code == "invalid_filter"


# --- build_offers_view ----------------------------------------------------------------------------


def _snapshot(name="runpod", status="ok", offers=(), error=None, partial=False, source_kind="env"):
    fetch = None
    if offers or status in ("ok", "partial", "stale"):
        stamp = datetime(2026, 11, 9, 14, 3, tzinfo=timezone.utc)
        fetch = cc.CatalogFetch(
            offers=tuple(offers),
            prices_as_of=stamp,
            stock_as_of=stamp,
            warnings=("a warning",) if partial else (),
            partial=partial,
            credential_source=source_kind,
        )
    return cc.ProviderSnapshot(
        name=name,
        status=status,
        fetch=fetch,
        error=error,
        checked_at=datetime(2026, 11, 9, 14, 3, tzinfo=timezone.utc),
        credential_source=source_kind,
        from_cache=False,
    )


def test_the_view_scales_filters_and_sorts_then_reports_ok_and_the_filters():
    rows = [
        _row(id="a", gpu_model="l40s-48gb", price_per_gpu_hour=0.9, vram_gb_per_gpu=48),
        _row(id="b", gpu_model="l40s-48gb", price_per_gpu_hour=0.5, vram_gb_per_gpu=48, stock_status="NONE"),
    ]
    view = cc.build_offers_view([_snapshot(offers=rows)], cc.OffersQuery(gpu_count=2, sort="price_per_hour"))
    assert view["ok"] is True and view["status"] == "ok"
    assert [o["id"] for o in view["offers"]] == ["b", "a"]
    assert view["offers"][0]["price_per_hour"] == pytest.approx(1.0)
    assert view["offers"][0]["gpu_count"] == 2
    assert view["gpu_models"] == [{"gpu_model": "l40s-48gb", "count": 2, "min_price_per_gpu_hour": 0.5}]
    assert view["filters_applied"]["gpu_count"] == 2
    assert view["providers"][0]["prices_as_of"] == "2026-11-09T14:03:00Z"
    assert view["error"] is None


def test_the_model_summary_is_computed_before_the_user_filters():
    rows = [_row(id="a", gpu_model="l40s-48gb"), _row(id="b", gpu_model="a100-80gb-sxm")]
    view = cc.build_offers_view([_snapshot(offers=rows)], cc.OffersQuery(gpu_model="l40s-48gb"))
    assert [o["id"] for o in view["offers"]] == ["a"]
    assert {m["gpu_model"] for m in view["gpu_models"]} == {"l40s-48gb", "a100-80gb-sxm"}


def test_a_not_configured_provider_gives_a_200_style_body_with_its_error_and_no_offers():
    error = cc.CatalogError("not_configured", cc.ERROR_MESSAGES["not_configured"])
    view = cc.build_offers_view([_snapshot(status="not_configured", error=error, source_kind=None)], cc.OffersQuery())
    assert view["ok"] is False and view["status"] == "not_configured"
    assert view["error"] == {"code": "not_configured", "message": cc.ERROR_MESSAGES["not_configured"]}
    assert view["offers"] == [] and view["count"] == 0


def test_the_worst_provider_status_becomes_the_overall_status():
    fresh = _snapshot(offers=[_row()], status="ok")
    partial = _snapshot(offers=[_row()], status="partial", partial=True)
    view = cc.build_offers_view([fresh, partial], cc.OffersQuery())
    assert view["status"] == "partial" and view["ok"] is True
    assert view["providers"][1]["warnings"] == ["a warning"]


def test_a_stale_provider_keeps_its_data_and_reports_the_latest_error():
    error = cc.CatalogError("rate_limited", cc.ERROR_MESSAGES["rate_limited"], 60)
    view = cc.build_offers_view([_snapshot(offers=[_row()], status="stale", error=error)], cc.OffersQuery())
    assert view["status"] == "stale" and view["ok"] is True
    assert view["offers"] and view["error"]["code"] == "rate_limited"


def test_an_empty_provider_list_is_an_error_status():
    view = cc.build_offers_view([], cc.OffersQuery())
    assert view["status"] == "error" and view["ok"] is False


def test_the_offer_row_keeps_unknown_values_as_null_not_zero():
    row = _row(stock_status=None, price_per_gpu_hour=None, vram_gb_per_gpu=None, data_center=None, gpu_variant=None)
    view = cc.build_offers_view([_snapshot(offers=[row])], cc.OffersQuery(sort="stock"))
    shaped = view["offers"][0]
    assert shaped["stock_status"] is None and shaped["price_per_gpu_hour"] is None
    assert shaped["vram_gb_per_gpu"] is None and shaped["ram_gb"] is None and shaped["vcpu"] is None


def test_the_cache_snapshot_offers_property_is_empty_without_a_fetch():
    snap = _snapshot(status="error", error=cc.CatalogError("unreachable", cc.ERROR_MESSAGES["unreachable"]))
    assert snap.offers == ()
