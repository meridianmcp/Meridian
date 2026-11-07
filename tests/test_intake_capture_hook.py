"""Tests for the UserPromptSubmit intake capture hook.

.claude/hooks/intake_capture.py reads the hook payload from stdin and appends one
JSON line per prompt to %LOCALAPPDATA%\\meridian\\intake\\queue.jsonl. It must
print nothing to stdout and exit 0 in every case. Each test points LOCALAPPDATA at
a pytest tmp directory so the owner's real queue is never touched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "intake_capture.py"


def _run_hook(stdin_bytes: bytes, local_appdata: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(local_appdata)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=stdin_bytes,
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )


def _queue(local_appdata: Path) -> Path:
    return local_appdata / "meridian" / "intake" / "queue.jsonl"


def _queue_lines(local_appdata: Path) -> list[str]:
    # Split on "\n" only: json.dumps(ensure_ascii=False) can emit characters such as
    # U+2028 that str.splitlines() would treat as line breaks.
    content = _queue(local_appdata).read_text(encoding="utf-8")
    assert content.endswith("\n")
    return content[:-1].split("\n")


def test_appends_one_valid_json_line_and_creates_directory(tmp_path):
    payload = {
        "session_id": "sess-abc",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "hello world",
    }
    result = _run_hook(json.dumps(payload).encode("utf-8"), tmp_path)

    assert result.returncode == 0
    assert result.stdout == b""
    lines = _queue_lines(tmp_path)
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["session_id"] == "sess-abc"
    assert record["prompt"] == "hello world"
    datetime.fromisoformat(record["timestamp"])  # raises if not an ISO timestamp


def test_each_prompt_appends_another_line(tmp_path):
    for prompt in ("first prompt", "second prompt"):
        payload = {"session_id": "sess-abc", "prompt": prompt}
        result = _run_hook(json.dumps(payload).encode("utf-8"), tmp_path)
        assert result.returncode == 0

    prompts = [json.loads(line)["prompt"] for line in _queue_lines(tmp_path)]
    assert prompts == ["first prompt", "second prompt"]


def test_multiline_unicode_prompt_stays_on_one_line(tmp_path):
    prompt = 'line one\nline "two" with \\ backslash\nunicode   ✓'
    payload = {"session_id": "sess-xyz", "prompt": prompt}
    result = _run_hook(json.dumps(payload).encode("utf-8"), tmp_path)

    assert result.returncode == 0
    assert result.stdout == b""
    lines = _queue_lines(tmp_path)
    assert len(lines) == 1
    assert json.loads(lines[0])["prompt"] == prompt


def test_malformed_stdin_exits_zero_with_stderr_only(tmp_path):
    result = _run_hook(b"this is not json", tmp_path)

    assert result.returncode == 0
    assert result.stdout == b""
    assert b"intake_capture" in result.stderr
    assert not _queue(tmp_path).exists()


def test_payload_without_prompt_writes_nothing(tmp_path):
    result = _run_hook(json.dumps({"session_id": "sess-abc"}).encode("utf-8"), tmp_path)

    assert result.returncode == 0
    assert result.stdout == b""
    assert not _queue(tmp_path).exists()
