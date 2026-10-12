"""Regression tests for the host-neutral usage ledger, slice 1 (824acdc6).

Covers the pure transcript parser, message-id dedupe, the price table loader,
the cache-aware cost formula and the local report. No DB and no network.

The FIXTURE rates below are test values chosen so the expected micros can be
checked by hand. They are not published prices.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from meridian.pricing import EMPTY_PRICE_TABLE, cost_micros, load_price_table
from meridian.usage_ledger import (
    UsageRecord,
    build_local_report,
    dedupe_by_message_id,
    parse_claude_code_jsonl,
)

FIXTURE = {
    "version": "fixture-2026-10",
    "models": {
        "fixture-model": {
            "input": "3",
            "output": "15",
            "cache_read": "0.3",
            "cache_write_5m": "3.75",
            "cache_write_1h": "6",
        }
    },
}


def _line(message_id, *, model="fixture-model", sidechain=False, usage=None, session="s1"):
    message = {"id": message_id, "model": model, "usage": usage}
    return json.dumps(
        {"type": "assistant", "sessionId": session, "isSidechain": sidechain, "message": message}
    )


def _entry(**overrides):
    entry = {
        "input": "1",
        "output": "1",
        "cache_read": "1",
        "cache_write_5m": "1",
        "cache_write_1h": "1",
    }
    entry.update(overrides)
    return entry


# --- price table loading ----------------------------------------------------


def test_load_price_table_stamps_version_and_keeps_decimal_rates():
    table = load_price_table(FIXTURE)
    assert table.version == "fixture-2026-10"
    price = table.lookup("fixture-model")
    assert price is not None
    assert price.input == Decimal("3")
    assert price.cache_read == Decimal("0.3")


def test_load_price_table_accepts_float_rates_via_decimal_string():
    table = load_price_table({"version": "v", "models": {"m": _entry(input=0.3)}})
    assert table.lookup("m").input == Decimal("0.3")


def test_lookup_is_exact_model_id_only():
    table = load_price_table(FIXTURE)
    assert table.lookup("fixture-model-2") is None


@pytest.mark.parametrize(
    "bad",
    [
        {"models": {}},
        {"version": "   ", "models": {}},
        {"version": "v", "models": []},
        {"version": "v", "models": {"m": "not-a-mapping"}},
        {"version": "v", "models": {" ": _entry()}},
        {"version": "v", "models": {"m": {k: "1" for k in ("input", "output", "cache_read", "cache_write_5m")}}},
        {"version": "v", "models": {"m": _entry(cache_write_5="1")}},
        {"version": "v", "models": {"m": _entry(input="-1")}},
        {"version": "v", "models": {"m": _entry(input="nan")}},
        {"version": "v", "models": {"m": _entry(input="abc")}},
        {"version": "v", "models": {"m": _entry(input=True)}},
    ],
)
def test_load_price_table_rejects_malformed_tables(bad):
    with pytest.raises(ValueError):
        load_price_table(bad)


def test_empty_table_prices_nothing():
    assert EMPTY_PRICE_TABLE.version == "unpopulated"
    assert EMPTY_PRICE_TABLE.lookup("fixture-model") is None
    record = UsageRecord(message_id="m", model="fixture-model", query_source="main", input_tokens=10)
    assert cost_micros(record, EMPTY_PRICE_TABLE) is None


# --- cost formula -----------------------------------------------------------


def test_cost_micros_is_cache_aware_and_exact():
    table = load_price_table(FIXTURE)
    record = UsageRecord(
        message_id="m",
        model="fixture-model",
        query_source="main",
        input_tokens=1_000_000,  # 3 * 1e6 = 3_000_000
        output_tokens=100_000,  # 15 * 1e5 = 1_500_000
        cache_read_tokens=2_000_000,  # 0.3 * 2e6 = 600_000
        cache_write_5m_tokens=100_000,  # 3.75 * 1e5 = 375_000
        cache_write_1h_tokens=50_000,  # 6 * 5e4 = 300_000
    )
    assert cost_micros(record, table) == 5_775_000


def test_cache_read_is_billed_at_cache_rate_not_input_rate():
    table = load_price_table(FIXTURE)
    record = UsageRecord(
        message_id="m", model="fixture-model", query_source="main", cache_read_tokens=1_000_000
    )
    assert cost_micros(record, table) == 300_000


def test_cost_rounds_half_up_once_on_the_total():
    table = load_price_table(
        {"version": "rounding", "models": {"m": _entry(input="0.5", output="0", cache_read="0", cache_write_5m="0", cache_write_1h="0")}}
    )

    def priced(tokens):
        return cost_micros(
            UsageRecord(message_id="x", model="m", query_source="main", input_tokens=tokens), table
        )

    assert priced(1) == 1  # 0.5 -> 1
    assert priced(3) == 2  # 1.5 -> 2
    assert priced(5) == 3  # 2.5 -> 3, half-up not half-even


def test_unknown_model_is_unpriced_not_zero():
    table = load_price_table(FIXTURE)
    record = UsageRecord(message_id="m", model="mystery", query_source="main", input_tokens=5)
    assert cost_micros(record, table) is None


# --- transcript parsing -----------------------------------------------------


def test_parse_splits_cache_writes_flags_sidechains_and_skips_non_usage_lines():
    lines = [
        _line(
            "msg_main",
            usage={
                "input_tokens": 7,
                "output_tokens": 9,
                "cache_read_input_tokens": 5,
                "cache_creation_input_tokens": 30,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 10,
                    "ephemeral_1h_input_tokens": 20,
                },
            },
        ),
        _line("msg_sub", sidechain=True, usage={"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 40}),
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}),
        "",
    ]
    result = parse_claude_code_jsonl(lines)
    assert result.malformed == 0
    assert len(result.records) == 2

    main, sub = result.records
    assert (main.message_id, main.query_source, main.session_id) == ("msg_main", "main", "s1")
    assert (main.input_tokens, main.output_tokens, main.cache_read_tokens) == (7, 9, 5)
    assert (main.cache_write_5m_tokens, main.cache_write_1h_tokens) == (10, 20)

    assert sub.query_source == "subagent"
    assert sub.cache_write_5m_tokens == 40  # no split: subagent writes default to 5m
    assert sub.cache_write_1h_tokens == 0


def test_main_cache_write_without_split_defaults_to_1h():
    result = parse_claude_code_jsonl([_line("legacy", usage={"cache_creation_input_tokens": 40})])
    record = result.records[0]
    assert (record.cache_write_5m_tokens, record.cache_write_1h_tokens) == (0, 40)


def test_split_shortfall_is_assigned_to_5m():
    usage = {
        "cache_creation_input_tokens": 25,
        "cache_creation": {"ephemeral_5m_input_tokens": 10, "ephemeral_1h_input_tokens": 0},
    }
    record = parse_claude_code_jsonl([_line("short", usage=usage)]).records[0]
    # 10 explicit 5m + 15 unsplit remainder of the 25 total -> 25 at 5m, 0 at 1h.
    assert (record.cache_write_5m_tokens, record.cache_write_1h_tokens) == (25, 0)


def test_malformed_lines_are_counted_never_raised():
    lines = [
        "{not json",
        "[1, 2]",
        json.dumps({"message": {"usage": {"input_tokens": 5}}}),  # usage without id
        json.dumps({"message": {"id": "a", "usage": {"input_tokens": "12"}}}),  # string count
        json.dumps({"message": {"id": "b", "usage": {"output_tokens": True}}}),  # bool count
        json.dumps({"message": {"id": "c", "usage": {"input_tokens": -1}}}),  # negative count
        json.dumps({"message": {"id": "d", "usage": {"cache_creation": "oops"}}}),
        json.dumps({"message": {"id": "e", "usage": "nope"}}),
    ]
    result = parse_claude_code_jsonl(lines)
    assert result.records == []
    assert result.malformed == 8


# --- dedupe -----------------------------------------------------------------


def test_dedupe_keeps_per_field_max_in_first_seen_order():
    lines = [
        _line("A", usage={"input_tokens": 7, "output_tokens": 10}),
        _line("B", usage={"input_tokens": 3, "output_tokens": 4}),
        _line("A", usage={"input_tokens": 7, "output_tokens": 250, "cache_read_input_tokens": 4}),
    ]
    deduped = dedupe_by_message_id(parse_claude_code_jsonl(lines).records)
    assert [r.message_id for r in deduped] == ["A", "B"]
    first = deduped[0]
    assert (first.input_tokens, first.output_tokens, first.cache_read_tokens) == (7, 250, 4)


# --- local report -----------------------------------------------------------


def _report_fixture():
    lines = [
        _line(
            "m1",
            usage={
                "input_tokens": 1_000_000,
                "output_tokens": 100_000,
                "cache_read_input_tokens": 2_000_000,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 100_000,
                    "ephemeral_1h_input_tokens": 50_000,
                },
            },
        ),
        _line("m1", usage={"input_tokens": 1_000_000, "output_tokens": 50_000}),  # repeat of m1
        _line("s1", model="mystery-model", sidechain=True, usage={"input_tokens": 500, "output_tokens": 500}),
    ]
    return parse_claude_code_jsonl(lines).records


def test_report_totals_priced_messages_and_flags_unpriced():
    report = build_local_report(
        _report_fixture(), load_price_table(FIXTURE), malformed_lines=2
    )
    assert report["price_table_version"] == "fixture-2026-10"
    assert report["messages"] == 2
    assert report["malformed_lines"] == 2
    assert report["unpriced_messages"] == 1
    assert report["cost_complete"] is False
    assert report["total_cost_micros"] == 5_775_000

    priced_row, unpriced_row = report["by_model_and_source"]
    assert (priced_row["model"], priced_row["query_source"]) == ("fixture-model", "main")
    assert priced_row["cost_micros"] == 5_775_000
    assert priced_row["unpriced_messages"] == 0
    assert (unpriced_row["model"], unpriced_row["query_source"]) == ("mystery-model", "subagent")
    assert unpriced_row["cost_micros"] is None
    assert unpriced_row["unpriced_messages"] == 1


def test_report_with_empty_table_is_fully_unpriced():
    report = build_local_report(_report_fixture(), EMPTY_PRICE_TABLE)
    assert report["price_table_version"] == "unpopulated"
    assert report["total_cost_micros"] == 0
    assert report["unpriced_messages"] == report["messages"] == 2
    assert report["cost_complete"] is False
    assert all(row["cost_micros"] is None for row in report["by_model_and_source"])


def test_report_is_json_serialisable_and_empty_input_is_complete():
    report = build_local_report(_report_fixture(), load_price_table(FIXTURE))
    assert json.loads(json.dumps(report)) == report

    empty = build_local_report([], load_price_table(FIXTURE))
    assert empty["messages"] == 0
    assert empty["cost_complete"] is True
    assert empty["total_cost_micros"] == 0
