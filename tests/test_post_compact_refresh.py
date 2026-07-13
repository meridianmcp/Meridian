"""3617361d — tests for the post-compaction SessionStart refresh hook.

Covers meridian/post_compact_refresh.py (the unit-testable core of the Claude
Code ``SessionStart`` hook that fires only on ``source == "compact"``) and
asserts the shipped ``.claude/hooks`` wrappers + settings wiring stay consistent
with it.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from meridian import post_compact_refresh as pcr

_REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# build_compact_refresh — the decision core                                    #
# --------------------------------------------------------------------------- #

def _ctx(out: dict) -> str:
    return out["hookSpecificOutput"]["additionalContext"]


def test_compact_source_injects_reminder():
    out = pcr.build_compact_refresh({"source": "compact"})
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    ctx = _ctx(out)
    assert ctx  # non-empty
    # It must nudge toward BOTH refresh tools and re-reading the sprint goal.
    assert "refresh_context" in ctx
    assert "refresh_tool_manifest" in ctx
    assert "sprint goal" in ctx


@pytest.mark.parametrize("source", ["startup", "resume", "clear", "", "unknown"])
def test_non_compact_sources_are_noop(source):
    out = pcr.build_compact_refresh({"source": source})
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert _ctx(out) == ""


@pytest.mark.parametrize("payload", [None, {}, [], "compact", 42, {"other": "x"}])
def test_malformed_payloads_fail_open(payload):
    # Must never raise; anything without source=="compact" is a no-op.
    out = pcr.build_compact_refresh(payload)
    assert _ctx(out) == ""


def test_missing_source_key_is_noop():
    assert _ctx(pcr.build_compact_refresh({"session_id": "abc"})) == ""


# --------------------------------------------------------------------------- #
# run() — stdin string -> stdout JSON string                                   #
# --------------------------------------------------------------------------- #

def test_run_parses_compact_stdin():
    result = json.loads(pcr.run(json.dumps({"source": "compact"})))
    assert "refresh_context" in _ctx(result)


def test_run_empty_stdin_is_noop():
    assert _ctx(json.loads(pcr.run(""))) == ""
    assert _ctx(json.loads(pcr.run("   "))) == ""


def test_run_unparseable_stdin_fails_open():
    assert _ctx(json.loads(pcr.run("{not json"))) == ""


def test_run_always_valid_json():
    # Every branch must yield a valid SessionStart envelope.
    for raw in ("", "{}", '{"source":"compact"}', '{"source":"resume"}', "garbage"):
        obj = json.loads(pcr.run(raw))
        assert obj["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert "additionalContext" in obj["hookSpecificOutput"]


# --------------------------------------------------------------------------- #
# main() via subprocess — exercises the real CLI entry, exits 0 (fail open)     #
# --------------------------------------------------------------------------- #

def test_main_subprocess_compact():
    proc = subprocess.run(
        [sys.executable, "-m", "meridian.post_compact_refresh"],
        input=json.dumps({"source": "compact"}),
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    assert proc.returncode == 0
    assert "refresh_context" in _ctx(json.loads(proc.stdout))


def test_main_subprocess_noncompact_noop():
    proc = subprocess.run(
        [sys.executable, "-m", "meridian.post_compact_refresh"],
        input=json.dumps({"source": "startup"}),
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    assert proc.returncode == 0
    assert _ctx(json.loads(proc.stdout)) == ""


# --------------------------------------------------------------------------- #
# Shipped hook artifacts stay consistent with the module + settings            #
# --------------------------------------------------------------------------- #

def test_hook_scripts_exist():
    assert (_REPO_ROOT / ".claude" / "hooks" / "post_compact_refresh.sh").is_file()
    assert (_REPO_ROOT / ".claude" / "hooks" / "post_compact_refresh.ps1").is_file()


def test_ps1_hook_is_ascii():
    # PS 5.1 reads BOM-less UTF-8 as cp1252; non-ASCII breaks the parser.
    data = (_REPO_ROOT / ".claude" / "hooks" / "post_compact_refresh.ps1").read_bytes()
    assert data.decode("ascii")  # raises if any byte is non-ASCII


def test_settings_wires_the_hook():
    settings = json.loads((_REPO_ROOT / ".claude" / "settings.json").read_text())
    session_start = settings["hooks"]["SessionStart"]
    joined = json.dumps(session_start)
    assert "post_compact_refresh.ps1" in joined
    # The wiring must target the compact matcher.
    assert any(
        entry.get("matcher") == "compact"
        and "post_compact_refresh" in json.dumps(entry.get("hooks", []))
        for entry in session_start
    )


def test_shell_and_module_agree_on_source_and_tools():
    # The dependency-free shell mirror must key off the same trigger + tools as
    # the Python core, so the two never drift.
    sh = (_REPO_ROOT / ".claude" / "hooks" / "post_compact_refresh.sh").read_text()
    assert '!= "compact"' in sh or 'compact' in sh
    assert "refresh_context" in sh
    assert "refresh_tool_manifest" in sh
