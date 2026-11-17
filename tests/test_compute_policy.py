"""Tests for meridian.compute_policy: objectives, precision trade-offs, confirmation rules, and revertible history.

Pure logic: no network and nothing billed. History files are written under tmp_path.
"""

from __future__ import annotations

import pytest

from meridian import compute_options as co
from meridian import compute_policy as cp

MODEL_13B = cp.ModelSpec(params_b=13)  # weights: fp16 26 GB, int8 13 GB, int4 6.5 GB; with overhead 31.2 / 15.6 / 7.8


def _offer(**kw) -> co.Offer:
    base = dict(id="o", provider="runpod", price_per_hour=1.0, vram_gb=24.0, ram_gb=64.0, vcpu=8)
    base.update(kw)
    return co.Offer(**base)


@pytest.fixture
def offers():
    return [
        _offer(id="big", price_per_hour=2.0, vram_gb=80, throughput=50),  # 2 h for 100 units, cost 4
        _offer(id="small", price_per_hour=0.6, vram_gb=24, throughput=20),  # 5 h, cost 3
        _offer(id="tiny", price_per_hour=0.3, vram_gb=16, throughput=8),  # 12.5 h, cost 3.75
    ]


WORK = cp.Workload(work_units=100)


# --- objectives ------------------------------------------------------------------------------------------


def test_cost_objective_picks_the_cheapest_option_that_fits_at_baseline_precision(offers):
    choice = cp.choose(MODEL_13B, offers, cp.Policy(), WORK)
    # fp16 needs 31.2 GB, so only "big" fits.
    assert choice.chosen.offer.id == "big"
    assert choice.chosen.precision == "fp16"
    assert choice.needs_human is False


def test_memory_objective_takes_a_lower_precision_only_when_the_policy_allows_it(offers):
    policy = cp.Policy(objective="memory", allowed_precisions=("fp16", "int8"))
    choice = cp.choose(MODEL_13B, offers, policy, WORK)
    assert choice.chosen.precision == "int8"
    assert choice.chosen.vram_needed_gb == pytest.approx(15.6)
    assert choice.chosen.offer.id == "small"  # same VRAM as "tiny" at int8, and cheaper overall
    assert choice.needs_human is True
    assert any("confirm the quality trade-off" in reason for reason in choice.reasons)


def test_memory_objective_does_not_quietly_quantise_when_int8_is_not_allowed(offers):
    choice = cp.choose(MODEL_13B, offers, cp.Policy(objective="memory"), WORK)
    assert all(option.precision in cp.BASELINE_PRECISIONS for option in choice.ranked)
    assert choice.chosen.precision == "fp16"


def test_min_throughput_floor_removes_slow_machines_from_the_memory_choice(offers):
    policy = cp.Policy(objective="memory", allowed_precisions=("fp16", "int8"), min_throughput=15)
    choice = cp.choose(MODEL_13B, offers, policy, WORK)
    assert choice.chosen.offer.id == "small"
    reasons = [reason for r in choice.rejected if r.offer.id == "tiny" for reason in r.reasons]
    assert any("below the 15 minimum" in reason for reason in reasons)


def test_throughput_objective_maximises_speed_inside_the_price_cap(offers):
    fast_policy = cp.Policy(objective="throughput", max_price_per_hour=3.0)
    assert cp.choose(MODEL_13B, offers, fast_policy, WORK).chosen.offer.id == "big"

    capped = cp.Policy(objective="throughput", max_price_per_hour=1.0, allowed_precisions=("int8",))
    choice = cp.choose(MODEL_13B, offers, capped, WORK)
    assert choice.chosen.offer.id == "small"  # 20 units/h is the fastest that costs no more than 1.0/h


def test_no_feasible_option_asks_for_a_human(offers):
    choice = cp.choose(MODEL_13B, offers, cp.Policy(max_price_per_hour=0.5), WORK)
    assert choice.chosen is None
    assert choice.needs_human is True
    assert "no offer meets the policy" in choice.reasons


def test_balanced_weights_move_the_pick_between_cost_and_memory(offers):
    policy_base = dict(objective="balanced", allowed_precisions=("fp16", "int8"))
    cost_heavy = cp.choose(MODEL_13B, offers, cp.Policy(weights=(1.0, 0.0, 0.0), **policy_base), WORK)
    memory_heavy = cp.choose(MODEL_13B, offers, cp.Policy(weights=(0.0, 1.0, 0.0), **policy_base), WORK)
    assert cost_heavy.chosen.offer.id == "small"  # cheapest total among the int8 options
    assert memory_heavy.chosen.vram_needed_gb == pytest.approx(15.6)  # lowest memory among the options


def test_unknown_throughput_marks_the_pick_as_a_guess_under_memory_objective():
    unmeasured = [_offer(id="x", price_per_hour=0.5, vram_gb=80)]
    choice = cp.choose(MODEL_13B, unmeasured, cp.Policy(objective="memory"), WORK)
    assert choice.chosen is not None
    assert choice.needs_human is True
    assert any("throughput is unknown" in reason for reason in choice.reasons)


def test_lenses_show_the_best_option_under_each_single_goal(offers):
    policy = cp.Policy(objective="balanced", allowed_precisions=("fp16", "int8"))
    choice = cp.choose(MODEL_13B, offers, policy, WORK)
    assert choice.cheapest.est_cost == pytest.approx(3.0)
    assert choice.lowest_memory.vram_needed_gb == pytest.approx(15.6)
    assert choice.fastest.offer.throughput == 50


# --- policy validation ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(objective="fast"),
        dict(allowed_precisions=()),
        dict(allowed_precisions=("fp8",)),
        dict(max_price_per_hour=0),
        dict(max_vram_gb=-1),
        dict(weights=(0.0, 0.0, 0.0)),
        dict(weights=(1.0, 1.0)),
    ],
)
def test_invalid_policies_are_rejected(kwargs):
    with pytest.raises(ValueError):
        cp.Policy(**kwargs).validate()


def test_policy_round_trips_through_its_dict_form():
    policy = cp.Policy(
        objective="balanced",
        max_price_per_hour=1.5,
        allowed_precisions=("fp16", "int8"),
        providers=("runpod",),
        weights=(2.0, 1.0, 0.5),
    )
    assert cp.Policy.from_dict(policy.to_dict()) == policy


# --- project history: set, revert, persist -----------------------------------------------------------------


def test_an_empty_history_means_the_safe_defaults():
    assert cp.current([]) == cp.Policy()


def test_set_and_revert_keep_every_revision_and_record_who_and_why():
    h = cp.set_policy([], cp.Policy(objective="cost"), actor="adam", reason="start", at="t1")
    h = cp.set_policy(h, cp.Policy(objective="memory", allowed_precisions=("fp16", "int8")),
                      actor="chat", reason="fit a 13B on a 24 GB card", at="t2")
    assert cp.current(h).objective == "memory"

    h = cp.revert(h, 1, actor="adam", reason="memory saving not worth the quality cost", at="t3")

    assert len(h) == 3
    assert cp.current(h) == cp.Policy(objective="cost")
    assert h[-1].reason == "revert to revision 1: memory saving not worth the quality cost"
    assert [r.actor for r in h] == ["adam", "chat", "adam"]


def test_a_change_without_an_actor_or_reason_is_refused():
    with pytest.raises(ValueError):
        cp.set_policy([], cp.Policy(), actor="", reason="x")
    with pytest.raises(ValueError):
        cp.set_policy([], cp.Policy(), actor="adam", reason="   ")
    with pytest.raises(ValueError):
        cp.revert([], 1, actor="adam", reason="x")


def test_history_round_trips_through_a_file(tmp_path):
    path = tmp_path / "policy" / "project.json"
    h = cp.set_policy([], cp.Policy(objective="throughput", max_price_per_hour=2.0), actor="adam", reason="first")
    cp.save_history(path, h)
    loaded = cp.load_history(path)
    assert loaded == h
    assert cp.current(loaded).objective == "throughput"


def test_a_missing_history_file_is_an_empty_history(tmp_path):
    assert cp.load_history(tmp_path / "none.json") == []
