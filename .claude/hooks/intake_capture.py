#!/usr/bin/env python3
# Claude Code UserPromptSubmit hook: append every prompt to a local intake queue.
#
# Reads the hook payload (JSON) from stdin and appends one JSON line per prompt to
#   %LOCALAPPDATA%\meridian\intake\queue.jsonl
# Each line holds session_id, the prompt text and an ISO timestamp (UTC).
#
# Dependency-free (stdlib only). Fails OPEN: any error is written to stderr and the
# script still exits 0, so a capture failure never blocks a prompt. Prints nothing
# to stdout, because UserPromptSubmit stdout is added to the model's context.
#
# Wire-in (already present in .claude/settings.json UserPromptSubmit):
#   & python "$env:CLAUDE_PROJECT_DIR/.claude/hooks/intake_capture.py"
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def queue_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "meridian" / "intake" / "queue.jsonl"


def main() -> int:
    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
        payload = json.loads(raw)
        prompt = payload.get("prompt")
        if not isinstance(prompt, str):
            print("intake_capture: hook payload has no string 'prompt'", file=sys.stderr)
            return 0
        record = {
            "session_id": payload.get("session_id"),
            "prompt": prompt,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        line = json.dumps(record, ensure_ascii=False)
        path = queue_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
    except Exception as exc:  # fail open: capture must never block a prompt
        print(f"intake_capture: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
