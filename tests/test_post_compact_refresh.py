"""3617361d — tests for the post-compaction SessionStart refresh hook.

Covers meridian/post_compact_refresh.py (the unit-testable core of the Claude
Code ``SessionStart`` hook that fires only on ``source == "compact"``) and
asserts the shipped ``.claude/hooks`` wrappers + settings wiring stay consistent
with it.
"""
from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from meridian import post_compact_refresh as pcr
from meridian.executor_config import normalize_executor_config

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

def _registered_hook_command(event: str, matcher: str = "") -> str:
    settings = json.loads((_REPO_ROOT / ".claude" / "settings.json").read_text())
    entries = settings["hooks"][event]
    entry = next(item for item in entries if item.get("matcher", "") == matcher)
    return entry["hooks"][0]["command"]


def _run_registered_hook(event: str, matcher: str, payload: dict, env: dict):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("PowerShell is required to execute the registered hook command")
    return subprocess.run(
        [shell, "-NoProfile", "-Command", _registered_hook_command(event, matcher)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        env=env,
        timeout=12,
    )


def test_main_subprocess_compact_runs_registered_command(tmp_path):
    env = os.environ.copy()
    env.update({
        "CLAUDE_PROJECT_DIR": str(_REPO_ROOT),
        "MERIDIAN_PROJECT_ID": "12345678-1234-1234-1234-123456789abc",
        "MERIDIAN_URL": "http://127.0.0.1:1",
        "LOCALAPPDATA": str(tmp_path),
    })
    proc = _run_registered_hook(
        "SessionStart",
        "compact",
        {"source": "compact", "session_id": "host-session-test", "cwd": str(_REPO_ROOT)},
        env,
    )
    assert proc.returncode == 0
    assert "refresh_context" in _ctx(json.loads(proc.stdout))


def test_registered_hooks_map_host_session_before_auto_checkpoint(tmp_path):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(size).decode("utf-8"))
            calls.append((self.path, self.headers.get("Authorization"), body))
            if self.path == "/hooks/session-start":
                reply = {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": "mapped session",
                    },
                    "meridian_session_id": "meridian-session-123",
                    "checkpoint_turns": 2,
                }
            else:
                reply = {"ok": True, "handoff": {"mode": "delta"}}
            encoded = json.dumps(reply).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        env = os.environ.copy()
        env.update({
            "CLAUDE_PROJECT_DIR": str(_REPO_ROOT),
            "MERIDIAN_PROJECT_ID": "12345678-1234-1234-1234-123456789abc",
            "MERIDIAN_URL": f"http://127.0.0.1:{server.server_port}",
            "MERIDIAN_TOKEN": "unit-test-token",
            "LOCALAPPDATA": str(tmp_path),
        })
        host_session_id = "host-session-raw-id-must-not-be-forwarded"
        payload = {
            "session_id": host_session_id,
            "cwd": str(_REPO_ROOT),
            "permission_mode": "bypassPermissions",
        }
        started = _run_registered_hook("SessionStart", "startup|resume", payload, env)
        assert started.returncode == 0
        assert "mapped session" in started.stdout
        assert calls[0][0] == "/hooks/session-start"
        assert calls[0][1] == "Bearer unit-test-token"
        start_body = calls[0][2]
        assert "session_id" not in start_body
        assert host_session_id not in json.dumps(start_body)
        assert start_body["session_name"].startswith("claude-hook-")
        assert start_body["mode"] == "continue"
        session_key = hashlib.sha256(host_session_id.encode("utf-8")).hexdigest()[:32]
        mapping_path = (
            tmp_path / "Meridian" / "hooks" / "12345678-1234-1234-1234-123456789abc"
            / f"{session_key}.json"
        )
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        assert mapping["project_id"] == "12345678-1234-1234-1234-123456789abc"
        assert mapping["meridian_session_id"] == "meridian-session-123"
        assert host_session_id not in mapping_path.name
        assert host_session_id not in mapping_path.read_text(encoding="utf-8")

        for _ in range(2):
            submitted = _run_registered_hook(
                "UserPromptSubmit", "", payload, env
            )
            assert submitted.returncode == 0

        stop_calls = [call for call in calls if call[0] == "/hooks/stop"]
        assert len(stop_calls) == 1
        stop_body = stop_calls[0][2]
        assert stop_calls[0][1] == "Bearer unit-test-token"
        assert stop_body["session_id"] == "meridian-session-123"
        assert stop_body["session_id"] != host_session_id
        assert "checkpoint_turns" not in stop_body
    finally:
        server.shutdown()
        worker.join(timeout=2)
        server.server_close()


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), ("12", 12), (True, None), (-1, None), (10001, None), ("bad", None), (2.5, None)],
)
def test_checkpoint_turns_config_is_bounded(value, expected):
    config = normalize_executor_config({"checkpoint_turns": value})
    assert config.get("checkpoint_turns") == expected
    if expected is None:
        assert "checkpoint_turns" not in config


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
