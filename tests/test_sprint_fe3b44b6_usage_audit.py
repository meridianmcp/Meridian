"""Regression tests for the transcript cost-audit module (sprint item fe3b44b6).

Every fixture is synthetic: a tiny session directory is built under pytest's
tmp_path. No network, no database, and no real transcript is read.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian import usage_audit as ua

SONNET = "claude-sonnet-5"
T0 = "2026-10-01T10:00:00Z"
SECRET = "SECRET-PAYLOAD-7f3a"


def _usage(**overrides):
    base = {"input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0}
    base.update(overrides)
    return base


def _line(msg_id, *, ts, usage, model=SONNET, content=None, **extra):
    obj = {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "id": msg_id,
            "model": model,
            "content": content if content is not None else [{"type": "text", "text": "ok"}],
            "usage": usage,
        },
    }
    obj.update(extra)
    return json.dumps(obj)


def _write(path: Path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _session(tmp_path: Path, main_lines, subagents=None) -> Path:
    """Build <tmp>/session with one main transcript and optional subagent files.

    ``subagents`` maps a path relative to ``subagents/`` to ``(lines, meta)``;
    ``meta`` is written as the sibling ``agent-<id>.meta.json`` when not None.
    """
    session = tmp_path / "session"
    _write(session / "main-session.jsonl", main_lines)
    for rel, (lines, meta) in (subagents or {}).items():
        sub_path = session / "subagents" / rel
        _write(sub_path, lines)
        if meta is not None:
            meta_path = sub_path.parent / (Path(rel).stem + ".meta.json")
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
    return session


# --- discovery -------------------------------------------------------------


def test_discover_finds_main_and_subagent_transcripts(tmp_path):
    session = _session(
        tmp_path,
        [_line("m1", ts=T0, usage=_usage(output_tokens=5))],
        {"explore/agent-a1.jsonl": ([_line("s1", ts=T0, usage=_usage(output_tokens=7))], None)},
    )
    main, subs = ua.discover_transcript_files(session)
    assert main.name == "main-session.jsonl"
    assert [p.name for p in subs] == ["agent-a1.jsonl"]


def test_discover_rejects_ambiguous_main_transcript(tmp_path):
    session = tmp_path / "s"
    _write(session / "one.jsonl", [])
    _write(session / "two.jsonl", [])
    with pytest.raises(ua.UsageAuditError, match="ambiguous"):
        ua.discover_transcript_files(session)


def test_explicit_main_path_resolves_ambiguity(tmp_path):
    session = tmp_path / "s"
    _write(session / "one.jsonl", [])
    _write(session / "two.jsonl", [])
    main, _ = ua.discover_transcript_files(session, session / "two.jsonl")
    assert main.name == "two.jsonl"


def test_missing_session_dir_is_a_usage_error(tmp_path):
    with pytest.raises(ua.UsageAuditError, match="does not exist"):
        ua.discover_transcript_files(tmp_path / "nope")


# --- pricing ---------------------------------------------------------------


def test_cost_uses_each_cache_tier_and_the_output_rate(tmp_path):
    usage = {
        "input_tokens": 1000,
        "cache_read_input_tokens": 4000,
        "output_tokens": 500,
        "cache_creation": {"ephemeral_5m_input_tokens": 2000, "ephemeral_1h_input_tokens": 3000},
    }
    report = ua.build_report(_session(tmp_path, [_line("m1", ts=T0, usage=usage)]))
    rates = ua.get_price_table().models[SONNET]
    t = report.totals
    assert (t.input_tokens, t.cache_write_5m_tokens, t.cache_write_1h_tokens) == (1000, 2000, 3000)
    assert (t.cache_read_tokens, t.output_tokens) == (4000, 500)
    expected = (
        1000 * rates.prompt_read_per_mtok
        + 2000 * rates.cache_write_5m_per_mtok
        + 3000 * rates.cache_write_1h_per_mtok
        + 4000 * rates.cache_read_per_mtok
        + 500 * rates.output_per_mtok
    ) / 1_000_000.0
    assert t.total_cost == pytest.approx(expected, rel=1e-9)


def test_flat_cache_creation_counts_as_five_minute_tier(tmp_path):
    usage = _usage(output_tokens=1, cache_creation_input_tokens=900)
    report = ua.build_report(_session(tmp_path, [_line("m1", ts=T0, usage=usage)]))
    assert report.totals.cache_write_5m_tokens == 900
    assert report.totals.cache_write_1h_tokens == 0


def test_web_search_requests_are_priced_per_thousand(tmp_path):
    usage = _usage(output_tokens=1, server_tool_use={"web_search_requests": 3})
    report = ua.build_report(_session(tmp_path, [_line("m1", ts=T0, usage=usage)]))
    table = ua.get_price_table()
    assert report.totals.web_search_requests == 3
    assert report.totals.web_search_cost == pytest.approx(3 / 1000 * table.web_search_per_1k_searches)


def test_unknown_model_is_counted_but_flagged_unpriced(tmp_path):
    line = _line("m1", ts=T0, usage=_usage(output_tokens=100), model="claude-mystery-9")
    report = ua.build_report(_session(tmp_path, [line]))
    assert report.unpriced_models == ["claude-mystery-9"]
    assert report.totals.output_tokens == 100
    assert report.totals.output_cost == 0.0
    assert any("claude-mystery-9" in c for c in report.caveats)


def test_dated_snapshot_suffix_resolves_to_base_model():
    table = ua.get_price_table()
    assert ua.resolve_model_key(f"{SONNET}-20260115", table) == SONNET
    assert ua.resolve_model_key("claude-mystery-9", table) is None


def test_unknown_price_table_version_is_rejected(tmp_path):
    session = _session(tmp_path, [_line("m1", ts=T0, usage=_usage(output_tokens=1))])
    with pytest.raises(ua.UsageAuditError, match="unknown price_table_version"):
        ua.build_report(session, price_table_version="1999-01-01")


def test_unverified_price_table_is_called_out_in_caveats(tmp_path):
    report = ua.build_report(_session(tmp_path, [_line("m1", ts=T0, usage=_usage(output_tokens=1))]))
    assert report.price_table_version == ua.CURRENT_PRICE_TABLE_VERSION
    assert any("NOT verified" in c for c in report.caveats)


# --- dedupe ----------------------------------------------------------------


def test_repeated_message_id_keeps_largest_usage_not_first_seen(tmp_path):
    first = _line("m1", ts=T0, usage=_usage(output_tokens=10, input_tokens=100))
    second = _line(
        "m1",
        ts=T0,
        usage=_usage(output_tokens=500, input_tokens=100),
        content=[{"type": "thinking", "thinking": "x" * 300}],
    )
    report = ua.build_report(_session(tmp_path, [first, second]))
    assert report.request_count["main"] == 1
    assert report.duplicate_message_ids_dropped == 1
    assert report.totals.output_tokens == 500


def test_same_message_in_main_and_subagent_is_counted_once(tmp_path):
    session = _session(
        tmp_path,
        [_line("m1", ts=T0, usage=_usage(output_tokens=40))],
        {"explore/agent-a1.jsonl": ([_line("m1", ts=T0, usage=_usage(output_tokens=40))], None)},
    )
    report = ua.build_report(session)
    assert report.request_count == {"main": 1, "subagent": 0, "total": 1}
    assert report.duplicate_message_ids_dropped == 1


# --- thinking share and effort ---------------------------------------------


def test_exact_thinking_tokens_drive_share_when_recorded(tmp_path):
    usage = _usage(output_tokens=1000, output_tokens_details={"thinking_tokens": 300})
    report = ua.build_report(_session(tmp_path, [_line("m1", ts=T0, usage=usage)]))
    assert report.thinking_share.exact_records == 1
    assert report.thinking_share.char_estimated_records == 0
    assert report.thinking_share.share_pct == pytest.approx(30.0)


def test_thinking_share_falls_back_to_character_estimate(tmp_path):
    content = [
        {"type": "thinking", "thinking": "a" * 300},
        {"type": "text", "text": "b" * 100},
    ]
    line = _line("m1", ts=T0, usage=_usage(output_tokens=1000), content=content)
    report = ua.build_report(_session(tmp_path, [line]))
    assert report.thinking_share.char_estimated_records == 1
    assert report.thinking_share.share_pct == pytest.approx(75.0)


def test_per_turn_effort_is_read_from_the_record(tmp_path):
    line = _line("m1", ts=T0, usage=_usage(output_tokens=1), perTurnEffort="xhigh")
    report = ua.build_report(_session(tmp_path, [line]))
    assert report.effort_distribution == {"xhigh": 1}


# --- phases, subagent labels, cost hygiene ---------------------------------


def test_mark_chapter_titles_assign_main_loop_phases(tmp_path):
    chapter = _line(
        "m2",
        ts=T0,
        usage=_usage(output_tokens=5),
        content=[
            {"type": "tool_use", "name": "mcp__ccd_session__mark_chapter", "input": {"title": "Implementation"}}
        ],
    )
    lines = [
        _line("m1", ts=T0, usage=_usage(output_tokens=1)),
        chapter,
        _line("m3", ts=T0, usage=_usage(output_tokens=7)),
    ]
    report = ua.build_report(_session(tmp_path, lines))
    assert report.by_phase["(session start)"].output_tokens == 1
    assert report.by_phase["Implementation"].output_tokens == 12


def test_subagent_meta_sidecar_labels_agent_and_workflow_phase(tmp_path):
    session = _session(
        tmp_path,
        [_line("m1", ts=T0, usage=_usage(output_tokens=1))],
        {
            "explore/agent-a1.jsonl": (
                [_line("s1", ts=T0, usage=_usage(input_tokens=10, output_tokens=40))],
                {"description": "Map the repo", "workflowPhase": "plan"},
            )
        },
    )
    report = ua.build_report(session)
    assert set(report.by_agent) == {"Map the repo"}
    assert report.by_phase["subagent:plan"].request_count == 1
    assert report.by_source["subagent"].request_count == 1


def test_subagent_without_meta_falls_back_to_directory_label(tmp_path):
    session = _session(
        tmp_path,
        [_line("m1", ts=T0, usage=_usage(output_tokens=1))],
        {"explore/agent-a1.jsonl": ([_line("s1", ts=T0, usage=_usage(output_tokens=2))], None)},
    )
    report = ua.build_report(session)
    assert set(report.by_agent) == {"explore"}
    assert "subagent:unphased" in report.by_phase


def test_startup_prefix_tax_multiplies_first_request_context(tmp_path):
    first = _line("m1", ts=T0, usage=_usage(input_tokens=100, cache_creation_input_tokens=900, output_tokens=1))
    second = _line("m2", ts=T0, usage=_usage(input_tokens=50, cache_read_input_tokens=1000, output_tokens=1))
    report = ua.build_report(_session(tmp_path, [first, second]))
    (entry,) = report.startup_prefix_tax
    assert entry.loop == "main"
    assert entry.first_request_context_tokens == 1000
    assert entry.request_count == 2
    assert entry.tax_tokens == 2000


def test_cache_gap_beyond_ttl_is_flagged_as_cold_rewrite(tmp_path):
    first = _line("m1", ts="2026-10-01T10:00:00Z", usage=_usage(output_tokens=1, cache_read_input_tokens=5000))
    second = _line("m2", ts="2026-10-01T10:20:00Z", usage=_usage(output_tokens=1, cache_creation_input_tokens=8000))
    report = ua.build_report(_session(tmp_path, [first, second]))
    assert len(report.cold_cache_rewrites) == 1
    event = report.cold_cache_rewrites[0]
    assert event.reason == "ttl_expired"
    assert event.gap_seconds == pytest.approx(1200.0)
    assert event.tokens_rewritten == 8000


# --- parsing robustness and honesty ----------------------------------------


def test_malformed_and_blank_lines_are_counted_not_fatal(tmp_path):
    lines = ["not json at all", "", _line("m1", ts=T0, usage=_usage(output_tokens=3))]
    report = ua.build_report(_session(tmp_path, lines))
    diag = report.parse_diagnostics[0]
    assert diag.lines_malformed_json == 1
    assert diag.lines_blank == 1
    assert diag.priced_records == 1


def _raw_assistant_line(msg_id, *, text, usage):
    """Like _line, but leaves non-ASCII characters unescaped in the JSON text."""
    obj = {
        "type": "assistant",
        "timestamp": T0,
        "message": {
            "id": msg_id,
            "model": SONNET,
            "content": [{"type": "text", "text": text}],
            "usage": usage,
        },
    }
    return json.dumps(obj, ensure_ascii=False)


@pytest.mark.parametrize(
    "separator", [" ", " ", "\u0085"], ids=["U+2028", "U+2029", "U+0085"]
)
def test_raw_unicode_separator_inside_a_json_string_keeps_the_record_whole(tmp_path, separator):
    line = _raw_assistant_line(
        "m1", text=f"before{separator}after", usage=_usage(input_tokens=7, output_tokens=42)
    )
    assert separator in line  # the character must reach the parser unescaped
    report = ua.build_report(_session(tmp_path, [line]))
    diag = report.parse_diagnostics[0]
    assert diag.lines_total == 1
    assert diag.lines_malformed_json == 0
    assert diag.priced_records == 1
    assert report.request_count["main"] == 1
    assert report.totals.input_tokens == 7
    assert report.totals.output_tokens == 42


def test_crlf_transcript_splits_on_newline_and_adds_no_phantom_line(tmp_path):
    session = tmp_path / "session"
    session.mkdir()
    first = _line("m1", ts=T0, usage=_usage(output_tokens=3))
    second = _line("m2", ts=T0, usage=_usage(output_tokens=4))
    (session / "main-session.jsonl").write_bytes(f"{first}\r\n{second}\r\n".encode("utf-8"))
    report = ua.build_report(session)
    diag = report.parse_diagnostics[0]
    assert diag.lines_total == 2
    assert diag.lines_blank == 0
    assert diag.lines_malformed_json == 0
    assert report.request_count["main"] == 2
    assert report.totals.output_tokens == 7


def test_reconciliation_flags_a_truncated_transcript(tmp_path):
    session = _session(tmp_path, [_line("m1", ts=T0, usage=_usage(output_tokens=500))])
    report = ua.build_report(session, reconcile_against=[{"message_id": "m1", "output_tokens": 10_000}])
    assert report.reconciliation.available is True
    assert report.reconciliation.looks_truncated is True
    assert report.reconciliation.matched_message_ids == 1
    assert any("looks truncated" in c for c in report.caveats)


def test_without_reconciliation_source_the_report_says_so(tmp_path):
    report = ua.build_report(_session(tmp_path, [_line("m1", ts=T0, usage=_usage(output_tokens=5))]))
    assert report.reconciliation.available is False
    assert "No cost-state" in " ".join(report.caveats)


def test_report_never_leaks_message_content(tmp_path):
    main = [
        _line(
            "m1",
            ts=T0,
            usage=_usage(output_tokens=9),
            content=[
                {"type": "text", "text": f"visible {SECRET}"},
                {"type": "thinking", "thinking": f"hidden {SECRET}"},
                {"type": "tool_use", "name": "Bash", "input": {"command": f"echo {SECRET}"}},
            ],
        )
    ]
    sub = [
        _line(
            "s1",
            ts=T0,
            usage=_usage(output_tokens=2),
            content=[{"type": "tool_use", "name": "Read", "input": {"file_path": SECRET}}],
        )
    ]
    report = ua.build_report(_session(tmp_path, main, {"explore/agent-a1.jsonl": (sub, None)}))
    blob = json.dumps(report.to_dict()) + ua.format_report_text(report)
    assert SECRET not in blob


# --- CLI -------------------------------------------------------------------


def test_cli_json_output_and_error_exit(tmp_path, capsys):
    session = _session(tmp_path, [_line("m1", ts=T0, usage=_usage(output_tokens=4))])
    assert ua.main([str(session), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["request_count"]["total"] == 1
    assert payload["price_table_version"] == ua.CURRENT_PRICE_TABLE_VERSION
    assert ua.main([str(tmp_path / "missing")]) == 1
    assert "error:" in capsys.readouterr().err
