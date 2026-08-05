"""6f465466 -- Read-only host-memory diagnostic: kernel pool vs Meridian-owned.

Covers:
1. ``_redact_secrets`` / ``_is_meridian_owned`` -- pure classification rules.
2. ``_kernel_pool_bytes_from_raw`` -- pure page-count -> byte-count math.
3. ``_pool_tag_struct_info`` / ``_parse_pool_tag_buffer`` -- pure struct-layout
   arithmetic and buffer parsing, exercised with synthetic bytes (no ctypes
   or real WinAPI call -- safe on any OS/CI).
4. ``_real_kernel_pool_totals`` / ``_real_pool_tag_attribution`` -- the
   non-Windows short-circuit (never touches ``ctypes.WinDLL`` off win32).
5. ``_assess`` -- pure kernel-vs-Meridian pressure classification + remediation.
6. ``collect_host_memory_diagnostic`` -- full composition, every data source
   injected (mocked), including failure-degrades-gracefully cases.
7. ``main`` -- CLI entry point, never raises, always exits 0, prints JSON.
"""
from __future__ import annotations

import json
import struct
import sys

import pytest

from meridian import host_memory_diagnostics as hmd


# ---------------------------------------------------------------------------
# 1. _redact_secrets / _is_meridian_owned
# ---------------------------------------------------------------------------


def test_redact_secrets_strips_bearer_token():
    text = "curl -H Authorization: Bearer abcdef0123456789ghij https://example.com"
    redacted = hmd._redact_secrets(text)
    assert "abcdef0123456789ghij" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_secrets_strips_basic_auth_url():
    text = "python -m tool --url https://user:hunter2@example.com/api"
    redacted = hmd._redact_secrets(text)
    assert "hunter2" not in redacted


def test_redact_secrets_empty_input():
    assert hmd._redact_secrets(None) == ""
    assert hmd._redact_secrets("") == ""


def test_redact_secrets_leaves_ordinary_cmdline_untouched():
    text = "pixi run python -m meridian --mcp"
    assert hmd._redact_secrets(text) == text


def test_is_meridian_owned_matches_meridian_module():
    assert hmd._is_meridian_owned("python.exe", "pixi run python -m meridian --mcp") is True


def test_is_meridian_owned_matches_codebase_memory():
    assert hmd._is_meridian_owned("codebase-memory-mcp.exe", "") is True


def test_is_meridian_owned_false_for_unrelated_process():
    assert hmd._is_meridian_owned("explorer.exe", "explorer.exe") is False
    assert hmd._is_meridian_owned("python.exe", "python train.py") is False


# ---------------------------------------------------------------------------
# 2. _kernel_pool_bytes_from_raw -- pure page->byte math
# ---------------------------------------------------------------------------


def test_kernel_pool_bytes_from_raw_basic_multiplication():
    result = hmd._kernel_pool_bytes_from_raw(
        kernel_paged_pages=1000, kernel_nonpaged_pages=500, kernel_total_pages=1500, page_size_bytes=4096
    )
    assert result == {
        "paged_bytes": 1000 * 4096,
        "nonpaged_bytes": 500 * 4096,
        "kernel_total_bytes": 1500 * 4096,
        "page_size_bytes": 4096,
    }


def test_kernel_pool_bytes_from_raw_zero_page_size_defaults_to_4096():
    result = hmd._kernel_pool_bytes_from_raw(1, 1, 2, 0)
    assert result["page_size_bytes"] == 4096
    assert result["paged_bytes"] == 4096


# ---------------------------------------------------------------------------
# 3. _pool_tag_struct_info / _parse_pool_tag_buffer -- pure struct parsing
# ---------------------------------------------------------------------------


def _pack_pool_tag_entry(tag: bytes, paged_used: int, nonpaged_used: int, pointer_size: int) -> bytes:
    """Build one raw SYSTEM_POOLTAG entry matching ``_pool_tag_struct_info``'s
    layout, for synthetic-buffer tests."""
    info = hmd._pool_tag_struct_info(pointer_size)
    buf = bytearray(info["entry_size"])
    buf[info["tag_off"] : info["tag_off"] + 4] = tag.ljust(4, b"\x00")[:4]
    size_fmt = "<Q" if pointer_size == 8 else "<L"
    struct.pack_into(size_fmt, buf, info["paged_used_off"], paged_used)
    struct.pack_into(size_fmt, buf, info["nonpaged_used_off"], nonpaged_used)
    return bytes(buf)


def _pack_pool_tag_buffer(entries: list[tuple[bytes, int, int]], pointer_size: int) -> bytes:
    info = hmd._pool_tag_struct_info(pointer_size)
    header = bytearray(info["header_size"])
    struct.pack_into("<L", header, 0, len(entries))
    body = b"".join(_pack_pool_tag_entry(tag, paged, nonpaged, pointer_size) for tag, paged, nonpaged in entries)
    return bytes(header) + body


@pytest.mark.parametrize("pointer_size", [8, 4])
def test_pool_tag_struct_info_header_and_entry_size_are_pointer_aligned(pointer_size):
    info = hmd._pool_tag_struct_info(pointer_size)
    assert info["header_size"] % pointer_size == 0
    assert info["entry_size"] % pointer_size == 0
    # Entry must be big enough to hold Tag(4) + 2*ULONG(4) + 2*pointer-size fields.
    assert info["entry_size"] >= 4 + 8 + 2 * pointer_size


@pytest.mark.parametrize("pointer_size", [8, 4])
def test_parse_pool_tag_buffer_round_trip(pointer_size):
    # Values kept under 4 GiB so they fit a 32-bit SIZE_T too (pointer_size=4).
    entries = [
        (b"Toke", 500 * 1024**2, 100),
        (b"Key ", 100 * 1024**2, 0),
        (b"VReg", 200 * 1024**2, 50),
    ]
    raw = _pack_pool_tag_buffer(entries, pointer_size)
    tags = hmd._parse_pool_tag_buffer(raw, pointer_size=pointer_size, top_n=15)
    assert [t["tag"] for t in tags] == ["Toke", "VReg", "Key"]
    assert tags[0]["paged_bytes"] == 500 * 1024**2
    assert tags[0]["nonpaged_bytes"] == 100


def test_parse_pool_tag_buffer_skips_zero_usage_tags():
    raw = _pack_pool_tag_buffer([(b"Dead", 0, 0), (b"Live", 10, 0)], pointer_size=8)
    tags = hmd._parse_pool_tag_buffer(raw, pointer_size=8)
    assert [t["tag"] for t in tags] == ["Live"]


def test_parse_pool_tag_buffer_respects_top_n():
    entries = [(f"T{i:03d}".encode()[:4], (i + 1) * 1024, 0) for i in range(20)]
    raw = _pack_pool_tag_buffer(entries, pointer_size=8)
    tags = hmd._parse_pool_tag_buffer(raw, pointer_size=8, top_n=5)
    assert len(tags) == 5
    # Descending by paged+nonpaged bytes -- highest index (largest value) first.
    assert tags[0]["tag"] == "T019"


def test_parse_pool_tag_buffer_truncated_input_degrades_to_empty():
    assert hmd._parse_pool_tag_buffer(b"", pointer_size=8) == []
    assert hmd._parse_pool_tag_buffer(b"\x00\x00", pointer_size=8) == []


def test_parse_pool_tag_buffer_count_exceeds_buffer_stops_early_not_raises():
    # Claims 1000 entries but buffer only has room for the header.
    info = hmd._pool_tag_struct_info(8)
    header = bytearray(info["header_size"])
    struct.pack_into("<L", header, 0, 1000)
    tags = hmd._parse_pool_tag_buffer(bytes(header), pointer_size=8)
    assert tags == []


def test_parse_pool_tag_buffer_malformed_input_never_raises():
    # Garbage bytes that don't represent a valid buffer -- must degrade, not throw.
    assert hmd._parse_pool_tag_buffer(b"\xff" * 3, pointer_size=8) == []


# ---------------------------------------------------------------------------
# 4. Non-Windows short-circuit -- must never touch ctypes.WinDLL off win32
# ---------------------------------------------------------------------------


def test_real_kernel_pool_totals_returns_none_off_windows(monkeypatch):
    monkeypatch.setattr(hmd.sys, "platform", "linux")
    assert hmd._real_kernel_pool_totals() is None


def test_real_pool_tag_attribution_returns_none_off_windows(monkeypatch):
    monkeypatch.setattr(hmd.sys, "platform", "linux")
    assert hmd._real_pool_tag_attribution() is None


# ---------------------------------------------------------------------------
# 5. _assess -- pure pressure classification + remediation
# ---------------------------------------------------------------------------


def _totals(rss=0, meridian_rss=0):
    return {"rss_bytes": rss, "meridian_owned_rss_bytes": meridian_rss}


def test_assess_kernel_pool_pressure_when_pool_dominates_unexplained_gap():
    system_memory = {"total_bytes": 32 * 1024**3, "available_bytes": 3 * 1024**3}
    kernel_pool = {"paged_bytes": 18 * 1024**3, "nonpaged_bytes": 3 * 1024**3}
    process_totals = _totals(rss=5 * 1024**3)
    result = hmd._assess(system_memory, kernel_pool, process_totals, pool_tags=None)
    assert result["assessment"]["kernel_pool_pressure"] is True
    assert result["assessment"]["meridian_process_pressure"] is False
    assert "OS-level" in result["remediation"]["recommendation"]
    assert result["remediation"]["auto_action_taken"] is False
    assert result["assessment"]["confidence"] == "medium"  # kernel_pool present, no tags


def test_assess_meridian_process_pressure_when_no_kernel_signal():
    process_totals = _totals(rss=2 * 1024**3, meridian_rss=int(1.5 * 1024**3))
    result = hmd._assess(system_memory=None, kernel_pool=None, process_totals=process_totals, pool_tags=None)
    assert result["assessment"]["kernel_pool_pressure"] is False
    assert result["assessment"]["meridian_process_pressure"] is True
    assert "Meridian-owned" in result["remediation"]["recommendation"]
    assert result["assessment"]["confidence"] == "low"  # kernel_pool absent entirely


def test_assess_both_pressures_recommends_meridian_first():
    system_memory = {"total_bytes": 32 * 1024**3, "available_bytes": 2 * 1024**3}
    kernel_pool = {"paged_bytes": 18 * 1024**3, "nonpaged_bytes": 3 * 1024**3}
    process_totals = _totals(rss=2 * 1024**3, meridian_rss=int(1.5 * 1024**3))
    result = hmd._assess(system_memory, kernel_pool, process_totals, pool_tags={"tags": [{"tag": "Toke"}]})
    assert result["assessment"]["kernel_pool_pressure"] is True
    assert result["assessment"]["meridian_process_pressure"] is True
    assert "Address the Meridian-owned processes first" in result["remediation"]["recommendation"]
    assert result["assessment"]["confidence"] == "high"  # kernel_pool + non-empty tags


def test_assess_neither_pressure_insufficient_evidence():
    process_totals = _totals(rss=1024, meridian_rss=0)
    result = hmd._assess(system_memory=None, kernel_pool=None, process_totals=process_totals, pool_tags=None)
    assert result["assessment"]["kernel_pool_pressure"] is False
    assert result["assessment"]["meridian_process_pressure"] is False
    assert "Insufficient evidence" in result["remediation"]["recommendation"]


def test_assess_kernel_pool_below_threshold_is_not_pressure():
    system_memory = {"total_bytes": 32 * 1024**3, "available_bytes": 20 * 1024**3}
    kernel_pool = {"paged_bytes": 100 * 1024**2, "nonpaged_bytes": 50 * 1024**2}  # well under 2 GiB
    process_totals = _totals(rss=1 * 1024**3)
    result = hmd._assess(system_memory, kernel_pool, process_totals, pool_tags=None)
    assert result["assessment"]["kernel_pool_pressure"] is False


def test_assess_never_auto_kills():
    result = hmd._assess(None, None, _totals(), None)
    assert result["remediation"]["auto_action_taken"] is False


# ---------------------------------------------------------------------------
# 6. collect_host_memory_diagnostic -- full composition, all sources injected
# ---------------------------------------------------------------------------


def _fake_samples():
    return [
        {"pid": 1, "name": "python.exe", "cmdline": "pixi run python -m meridian --mcp", "rss_bytes": 500_000_000, "private_bytes": 400_000_000},
        {"pid": 2, "name": "codebase-memory-mcp.exe", "cmdline": "", "rss_bytes": 300_000_000, "private_bytes": 250_000_000},
        {"pid": 3, "name": "chrome.exe", "cmdline": "chrome --profile=default", "rss_bytes": 1_000_000_000, "private_bytes": 900_000_000},
    ]


def test_collect_host_memory_diagnostic_aggregates_process_totals_and_meridian_split():
    diag = hmd.collect_host_memory_diagnostic(
        process_samples_fn=_fake_samples,
        system_memory_fn=lambda: {"total_bytes": 10 * 1024**3, "available_bytes": 5 * 1024**3, "percent_used": 50.0},
        kernel_pool_fn=lambda: None,
        pool_tag_fn=lambda: None,
        os_info_fn=lambda: {"platform": "win32"},
        now_fn=lambda: 1_700_000_000.0,
    )
    assert diag["schema_version"] == hmd.SCHEMA_VERSION
    assert diag["process_totals"]["count"] == 3
    assert diag["process_totals"]["rss_bytes"] == 1_800_000_000
    assert diag["process_totals"]["meridian_owned_count"] == 2
    assert diag["process_totals"]["meridian_owned_rss_bytes"] == 800_000_000
    assert diag["os"] == {"platform": "win32"}
    assert diag["kernel_pool"] is None
    assert diag["pool_tag_attribution"] is None
    assert diag["remediation"]["auto_action_taken"] is False
    # Top processes sorted descending by RSS.
    assert diag["process_totals"]["top_processes"][0]["pid"] == 3
    assert diag["timestamp"].startswith("2023-")  # 1_700_000_000 epoch -> 2023


def test_collect_host_memory_diagnostic_redacts_cmdline_secrets():
    def _samples():
        return [
            {"pid": 9, "name": "curl.exe", "cmdline": "curl -H Authorization: Bearer abcdef0123456789ghij", "rss_bytes": 1, "private_bytes": 1}
        ]

    diag = hmd.collect_host_memory_diagnostic(
        process_samples_fn=_samples,
        system_memory_fn=lambda: None,
        kernel_pool_fn=lambda: None,
        pool_tag_fn=lambda: None,
    )
    cmdline = diag["process_totals"]["top_processes"][0]["cmdline"]
    assert "abcdef0123456789ghij" not in cmdline


def test_collect_host_memory_diagnostic_degrades_when_every_source_raises():
    def _boom():
        raise RuntimeError("simulated failure")

    diag = hmd.collect_host_memory_diagnostic(
        process_samples_fn=_boom,
        system_memory_fn=_boom,
        kernel_pool_fn=_boom,
        pool_tag_fn=_boom,
        os_info_fn=_boom,
    )
    assert diag["process_totals"]["count"] == 0
    assert diag["system_memory"] is None
    assert diag["kernel_pool"] is None
    assert diag["pool_tag_attribution"] is None
    assert diag["os"] == {"platform": sys.platform}
    assert diag["remediation"]["auto_action_taken"] is False


def test_collect_host_memory_diagnostic_is_json_serializable():
    diag = hmd.collect_host_memory_diagnostic(
        process_samples_fn=_fake_samples,
        system_memory_fn=lambda: {"total_bytes": 1, "available_bytes": 1, "percent_used": 1.0},
        kernel_pool_fn=lambda: {"paged_bytes": 1, "nonpaged_bytes": 1, "kernel_total_bytes": 2, "page_size_bytes": 4096, "source": "GetPerformanceInfo"},
        pool_tag_fn=lambda: {"source": "NtQuerySystemInformation", "tags": [{"tag": "Toke", "paged_bytes": 1, "nonpaged_bytes": 0}]},
    )
    json.dumps(diag)  # must not raise


def test_collect_host_memory_diagnostic_defaults_use_real_functions(monkeypatch):
    """Sanity check that omitting every *_fn falls back to the real
    (psutil/ctypes-backed) implementations rather than erroring -- exercised
    with psutil itself absent so this stays fast and deterministic in CI."""
    monkeypatch.setattr(hmd.sys, "platform", "linux")
    diag = hmd.collect_host_memory_diagnostic()
    assert diag["kernel_pool"] is None
    assert diag["pool_tag_attribution"] is None
    assert isinstance(diag["process_totals"]["count"], int)


# ---------------------------------------------------------------------------
# 7. main() -- CLI entry point
# ---------------------------------------------------------------------------


def test_main_prints_valid_json_and_returns_zero(monkeypatch, capsys):
    monkeypatch.setattr(hmd, "collect_host_memory_diagnostic", lambda: {"schema_version": 1, "ok": True})
    rc = hmd.main([])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed == {"schema_version": 1, "ok": True}


def test_main_pretty_flag_indents_output(monkeypatch, capsys):
    monkeypatch.setattr(hmd, "collect_host_memory_diagnostic", lambda: {"a": 1})
    hmd.main(["--pretty"])
    out = capsys.readouterr().out
    assert "\n" in out


def test_main_never_raises_when_collection_fails(monkeypatch, capsys):
    def _boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(hmd, "collect_host_memory_diagnostic", _boom)
    rc = hmd.main([])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["error"] == "collection_failed"
