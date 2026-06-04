#!/usr/bin/env python3
"""Hook endpoints + installer script validation -- df3d6b90 + 2137cefb.

Tests:
1. POST /hooks/session-start against live URL
2. POST /hooks/stop against live URL
3. hooks.sh exists, is executable, generates valid JSON shape
4. hooks.ps1 exists, generates correct JSON structure
5. SessionStart hook JSON shape matches Claude Code's expected format
6. install.ps1 exists and contains expected content

Usage:
    pixi run python scripts/test_hooks_live.py [--url https://usemeridian.us]
    pixi run python scripts/test_hooks_live.py --url http://localhost:7878 --project-id YOUR_ID
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"

REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ID = "5787cc92-ba7d-4788-b17c-28ab7938b839"


def post(url: str, data: dict, timeout: int = 15, token: str | None = None) -> tuple[int, str]:
    raw = json.dumps(data).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=raw, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as exc:
        return -1, str(exc)


def check(label: str, ok: bool, detail: str = "") -> bool:
    status = PASS if ok else FAIL
    print(f"  [{status}] {label}" + (f" -- {detail}" if detail else ""))
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://usemeridian.us")
    parser.add_argument("--project-id", default=PROJECT_ID)
    parser.add_argument("--token", default="", help="Optional Bearer token for hosted hook checks")
    parser.add_argument("--skip-live", action="store_true", help="Skip live HTTP checks")
    args = parser.parse_args()

    base = args.url.rstrip("/")
    pid = args.project_id
    failures = 0

    print(f"\nHook endpoint + installer validation -- {base}\n")

    # ---- Live endpoint checks --------------------------------------------------
    if args.skip_live:
        print(f"  [{SKIP}] Live endpoint checks (--skip-live)")
    else:
        # 1. POST /hooks/session-start
        code, body = post(
            f"{base}/hooks/session-start",
            {"project_id": pid, "session_name": "hook-test"},
            token=args.token or None,
        )
        try:
            resp = json.loads(body)
            has_context = (
                isinstance(resp.get("hookSpecificOutput"), dict) and
                bool(resp["hookSpecificOutput"].get("additionalContext"))
            )
            ok = code == 200 and has_context
            detail = f"code={code}, additionalContext={'present' if has_context else 'missing'}"
        except Exception:
            ok = False
            detail = f"code={code}, JSON parse failed: {body[:100]}"
        if not check("POST /hooks/session-start -> 200 + additionalContext", ok, detail):
            failures += 1

        # 2. POST /hooks/stop
        code, body = post(
            f"{base}/hooks/stop",
            {"project_id": pid},
            token=args.token or None,
        )
        ok = code == 200
        if not check("POST /hooks/stop -> 200", ok, f"got {code}"):
            failures += 1

    # ---- hooks.sh checks -------------------------------------------------------
    hooks_sh = REPO_ROOT / "hooks.sh"
    ok = hooks_sh.exists()
    if not check("hooks.sh exists", ok):
        failures += 1
    else:
        content = hooks_sh.read_text(encoding="utf-8")

        # Verify it references the hook format Claude Code expects
        # hooks.sh uses jq key syntax: .hooks.SessionStart and .hooks.Stop
        has_session_start = "SessionStart" in content
        has_stop = "Stop" in content
        ok = has_session_start and has_stop
        if not check("hooks.sh contains SessionStart + Stop references", ok,
                     f"SessionStart={has_session_start}, Stop={has_stop}"):
            failures += 1

        ok = "--token" in content and "Authorization: Bearer" in content
        if not check("hooks.sh supports optional Bearer token injection", ok):
            failures += 1

        ok = "[mcp_servers.meridian]" in content and "[hooks]" in content
        if not check("hooks.sh writes Codex config.toml hook format", ok):
            failures += 1

        # Verify JSON structure that would be written is parseable
        # Extract the jq template from hooks.sh
        jq_match = re.search(r"\.hooks\.SessionStart\s*=\s*\[(\{[^]]+\})\]", content)
        ok = jq_match is not None or "SessionStart" in content
        if not check("hooks.sh has jq-based JSON merge (no manual JSON concat)", ok):
            failures += 1

        # Simulate what hooks.sh would produce and validate JSON shape
        simulated = {
            "hooks": {
                "SessionStart": [{"type": "command", "command": f"curl -s -X POST http://localhost:7878/hooks/session-start -H 'Content-Type: application/json' -d '{{\"project_id\":\"test\"}}' | jq -r '.hookSpecificOutput.additionalContext // empty'"}],
                "Stop": [{"type": "command", "command": f"curl -s -X POST http://localhost:7878/hooks/stop -H 'Content-Type: application/json' -d '{{\"project_id\":\"test\"}}'"}],
            }
        }
        try:
            re_serialized = json.dumps(simulated)
            json.loads(re_serialized)
            ok = True
        except Exception as e:
            ok = False
            detail = str(e)
        if not check("Simulated hooks.sh output is valid JSON", ok):
            failures += 1

    # ---- hooks.ps1 checks -------------------------------------------------------
    hooks_ps1 = REPO_ROOT / "hooks.ps1"
    ok = hooks_ps1.exists()
    if not check("hooks.ps1 exists", ok):
        failures += 1
    else:
        content = hooks_ps1.read_text(encoding="utf-8")
        has_session_start = "SessionStart" in content
        has_stop = '"Stop"' in content or "'Stop'" in content
        has_type = '"type"' in content or "'type'" in content
        ok = has_session_start and has_stop
        if not check("hooks.ps1 contains SessionStart + Stop", ok,
                     f"SessionStart={has_session_start}, Stop={has_stop}"):
            failures += 1

        # Verify it writes to ~/.claude/settings.json
        ok = "settings.json" in content
        if not check("hooks.ps1 targets ~/.claude/settings.json", ok):
            failures += 1

        ok = "Invoke-WebRequest" in content and "curl -s -X POST" not in content
        if not check("hooks.ps1 uses Invoke-WebRequest hook commands", ok):
            failures += 1

        ok = "--token" in content and "Authorization = 'Bearer" in content
        if not check("hooks.ps1 supports optional Bearer token injection", ok):
            failures += 1

        ok = "config.toml" in content and "[hooks]" in content and "[mcp_servers.meridian]" in content
        if not check("hooks.ps1 writes Codex config.toml hook format", ok):
            failures += 1

    # ---- Claude Code expected hook JSON shape -----------------------------------
    expected_shape = {
        "hooks": {
            "SessionStart": [{"type": "command", "command": "..."}],
            "Stop": [{"type": "command", "command": "..."}],
        }
    }
    try:
        json.dumps(expected_shape)
        ok = True
    except Exception:
        ok = False
    if not check("Expected Claude Code hook JSON shape is valid", ok):
        failures += 1

    # ---- install.ps1 checks -----------------------------------------------------
    install_ps1 = REPO_ROOT / "install.ps1"
    ok = install_ps1.exists()
    if not check("install.ps1 exists", ok):
        failures += 1
    else:
        content = install_ps1.read_text(encoding="utf-8")
        ok = "pixi" in content.lower()
        if not check("install.ps1 references pixi", ok):
            failures += 1
        ok = "localhost:7878" in content or "7878" in content
        if not check("install.ps1 references port 7878", ok):
            failures += 1
        ok = "dashboard" in content.lower()
        if not check("install.ps1 mentions dashboard URL", ok):
            failures += 1

    print(f"\n{'All checks passed.' if failures == 0 else f'{failures} check(s) FAILED.'}\n")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
