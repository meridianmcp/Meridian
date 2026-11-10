"""Tests for meridian.compute_options: the LLM memory estimate, offer ranking, and catalog loading.

Pure logic throughout: no network, no provider SDK, nothing billed. Catalogs are written to tmp_path.
"""

from __future__ import annotations

import json

import pytest

from meridian import compute_options as co


def _offer(**kw) -> co.Offer:
    base = dict(id="o", provider="runpod", price_per_hour=1.0, vram_gb=24.0, ram_gb=64.0, vcpu=8)
    base.update(kw)
    return co.Offer(**base)


# --- estimate_llm_vram_gb -------------------------------------------------------------------------


def test_weights_only_estimate_is_params_times_bytes_times_overhead():
    assert co.estimate_llm_vram_gb(7, "fp16") == pytest.approx(16.8)
    assert co.estimate_llm_vram_gb(7, "int4") == pytest.approx(4.2)


def test_kv_cache_is_counted_only_when_the_architecture_is_given():
    with_kv = co.estimate_llm_vram_gb(
        7, "fp16", context_tokens=4096, n_layers=32, n_kv_heads=32, head_dim=128
    )
    without_arch = co.estimate_llm_vram_gb(7, "fp16", context_tokens=4096)
    assert with_kv == pytest.approx(19.38, abs=0.01)
    assert without_arch == pytest.approx(16.8)


def test_estimate_rejects_bad_inputs():
    with pytest.raises(ValueError):
        co.estimate_llm_vram_gb(0)
    with pytest.raises(ValueError):
        co.estimate_llm_vram_gb(7, "fp8")
    with pytest.raises(ValueError):
        co.estimate_llm_vram_gb(7, batch_size=0)
    with pytest.raises(ValueError):
        co.estimate_llm_vram_gb(7, overhead=0.9)


# --- rank_offers ----------------------------------------------------------------------------------


@pytest.fixture
def catalog():
    return [
        _offer(id="A", price_per_hour=1.0, vram_gb=24, throughput=10),  # 10 h, cost 10
        _offer(id="B", price_per_hour=2.0, vram_gb=80, throughput=50),  # 2 h, cost 4
        _offer(id="C", price_per_hour=0.5, vram_gb=16, throughput=5),  # too little VRAM for the job
        _offer(id="D", price_per_hour=0.8, vram_gb=24),  # no throughput, so time unknown
        _offer(id="E", price_per_hour=0.1, vram_gb=80, throughput=100, available=False),
    ]


def test_ranks_feasible_offers_by_estimated_total_cost_and_explains_rejections(catalog):
    job = co.Job(min_vram_gb=20, work_units=100)
    ranked, rejected = co.rank_offers(catalog, job)

    assert [r.offer.id for r in ranked] == ["B", "A", "D"]
    assert ranked[0].est_hours == pytest.approx(2.0)
    assert ranked[0].est_cost == pytest.approx(4.0)
    assert ranked[2].est_cost is None
    assert any("time unknown" in note for note in ranked[2].notes)

    reasons = {r.offer.id: r.reasons for r in rejected}
    assert any("VRAM 16 GB is below" in reason for reason in reasons["C"])
    assert any("not available" in reason for reason in reasons["E"])


def test_the_shorter_run_breaks_a_tie_on_total_cost():
    fast = _offer(id="fast", price_per_hour=2.0, throughput=20)  # 5 h, cost 10
    slow = _offer(id="slow", price_per_hour=1.0, throughput=10)  # 10 h, cost 10
    ranked, _ = co.rank_offers([slow, fast], co.Job(work_units=100))
    assert [r.offer.id for r in ranked] == ["fast", "slow"]


def test_max_hours_drops_offers_that_run_too_long(catalog):
    job = co.Job(min_vram_gb=20, work_units=100, max_hours=5)
    ranked, rejected = co.rank_offers(catalog, job)
    assert [r.offer.id for r in ranked] == ["B", "D"]
    assert any("above the 5 h limit" in reason for r in rejected if r.offer.id == "A" for reason in r.reasons)


def test_max_price_per_hour_is_a_hard_cap(catalog):
    job = co.Job(min_vram_gb=20, work_units=100, max_price_per_hour=1.5)
    ranked, rejected = co.rank_offers(catalog, job)
    assert [r.offer.id for r in ranked] == ["A", "D"]
    assert any("above the 1.5/h cap" in reason for r in rejected if r.offer.id == "B" for reason in r.reasons)


def test_provider_allow_list_excludes_other_providers():
    offers = [_offer(id="rp", provider="runpod"), _offer(id="other", provider="vastai")]
    ranked, rejected = co.rank_offers(offers, co.Job(providers=("runpod",)))
    assert [r.offer.id for r in ranked] == ["rp"]
    assert rejected[0].offer.id == "other"


def test_cheapest_returns_the_best_feasible_offer_or_none(catalog):
    assert co.cheapest(catalog, co.Job(min_vram_gb=20, work_units=100)).offer.id == "B"
    assert co.cheapest(catalog, co.Job(min_vram_gb=500)) is None


def test_empty_job_keeps_every_available_offer_ranked_by_price():
    offers = [_offer(id="x", price_per_hour=3.0), _offer(id="y", price_per_hour=1.0)]
    ranked, rejected = co.rank_offers(offers, co.Job())
    assert [r.offer.id for r in ranked] == ["y", "x"]
    assert rejected == []


# --- catalog loading ------------------------------------------------------------------------------


def test_load_catalog_reads_offers_and_inherits_the_snapshot_date(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps(
            {
                "as_of": "2026-10-24",
                "offers": [
                    {"id": "a100", "provider": "runpod", "price_per_hour": 1.19, "vram_gb": 80, "throughput": 3.0},
                    {"id": "4090", "provider": "runpod", "price_per_hour": 0.34, "vram_gb": 24},
                ],
            }
        ),
        encoding="utf-8",
    )
    offers = co.load_catalog(path)
    assert [o.id for o in offers] == ["a100", "4090"]
    assert offers[0].price_as_of == "2026-10-24"
    assert offers[1].throughput is None


@pytest.mark.parametrize(
    "entry",
    [
        {"id": "x", "provider": "runpod"},  # no price
        {"id": "x", "provider": "runpod", "price_per_hour": 0},
        {"id": "x", "provider": "runpod", "price_per_hour": 1.0, "vram_gb": -1},
        {"id": "x", "provider": "runpod", "price_per_hour": 1.0, "throughput": 0},
        {"id": "", "provider": "runpod", "price_per_hour": 1.0},
    ],
)
def test_malformed_catalog_entries_are_rejected(entry):
    with pytest.raises(ValueError):
        co.offer_from_dict(entry)


def test_a_catalog_without_an_offers_list_is_rejected(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"as_of": "2026-10-24"}), encoding="utf-8")
    with pytest.raises(ValueError):
        co.load_catalog(path)


# --- catalog fields and require_price (GPU offer catalog, pass 1) -------------------------------


def test_trailing_catalog_fields_default_to_none_so_existing_offers_are_unchanged():
    offer = _offer()
    for name in (
        "gpu_model",
        "gpu_variant",
        "gpu_count",
        "max_gpu_count",
        "price_per_gpu_hour",
        "vram_gb_per_gpu",
        "cloud_type",
        "data_center",
        "stock_status",
    ):
        assert getattr(offer, name) is None, name
    # Existing meanings are unchanged: available still defaults to True, and the totals keep their values.
    assert offer.available is True
    assert offer.price_per_hour == 1.0
    assert offer.vram_gb == 24.0


def test_existing_positional_and_keyword_constructors_still_work():
    positional = co.Offer("o", "runpod", 1.5, 24.0, 64.0, 8, "gpu", 100.0, False, "2026-11-09T14:03:00Z")
    assert positional.available is False
    assert positional.price_as_of == "2026-11-09T14:03:00Z"
    assert co.Offer(id="o", provider="runpod", price_per_hour=1.0) == co.Offer(
        id="o", provider="runpod", price_per_hour=1.0
    )


def test_unknown_is_none_not_zero_for_catalog_style_offers():
    offer = co.Offer(id="o", provider="runpod", price_per_hour=None, vram_gb=None, ram_gb=None, vcpu=None)
    assert offer.price_per_hour is None and offer.vram_gb is None
    assert offer.ram_gb is None and offer.vcpu is None


def test_require_price_false_accepts_a_missing_or_null_price_as_none():
    assert co.offer_from_dict({"id": "x", "provider": "runpod"}, require_price=False).price_per_hour is None
    assert (
        co.offer_from_dict({"id": "x", "provider": "runpod", "price_per_hour": None}, require_price=False)
        .price_per_hour
        is None
    )


def test_require_price_true_is_the_default_and_still_rejects_a_missing_price():
    with pytest.raises(ValueError):
        co.offer_from_dict({"id": "x", "provider": "runpod"})
    with pytest.raises(ValueError):
        co.offer_from_dict({"id": "x", "provider": "runpod", "price_per_hour": None})


@pytest.mark.parametrize("require_price", [True, False])
@pytest.mark.parametrize("bad_price", [0, -1.0, float("nan"), float("inf")])
def test_a_present_non_positive_or_non_finite_price_is_rejected_in_both_modes(bad_price, require_price):
    entry = {"id": "x", "provider": "runpod", "price_per_hour": bad_price}
    with pytest.raises(ValueError):
        co.offer_from_dict(entry, require_price=require_price)


def test_rank_offers_rejects_unknown_price_and_unknown_stock_without_crashing():
    unknown_price = co.Offer(id="p", provider="runpod", price_per_hour=None, vram_gb=24.0)
    unknown_stock = co.Offer(id="s", provider="runpod", price_per_hour=1.0, vram_gb=24.0, available=None)
    ranked, rejected = co.rank_offers([unknown_price, unknown_stock, _offer(id="ok")], co.Job(min_vram_gb=0))
    assert [r.offer.id for r in ranked] == ["ok"]
    reasons = {r.offer.id: r.reasons for r in rejected}
    assert "price is not reported" in reasons["p"]
    assert "stock is unknown" in reasons["s"]


def test_unreported_vram_ram_or_vcpu_is_rejected_only_when_the_job_needs_it():
    no_vram = co.Offer(id="v", provider="runpod", price_per_hour=1.0, vram_gb=None, ram_gb=None, vcpu=None)
    ranked, rejected = co.rank_offers([no_vram], co.Job())
    assert [r.offer.id for r in ranked] == ["v"] and not rejected
    _, rejected = co.rank_offers([no_vram], co.Job(min_vram_gb=24, min_ram_gb=8, min_vcpu=4))
    text = " ".join(rejected[0].reasons)
    assert "VRAM is not reported" in text and "RAM is not reported" in text and "vCPU is not reported" in text


def test_a_known_price_below_a_cap_still_ranks_as_before():
    cheap = _offer(id="cheap", price_per_hour=0.5)
    dear = _offer(id="dear", price_per_hour=2.0)
    ranked, rejected = co.rank_offers([dear, cheap], co.Job(max_price_per_hour=1.0))
    assert [r.offer.id for r in ranked] == ["cheap"]
    assert "above the 1/h cap" in rejected[0].reasons[0]
