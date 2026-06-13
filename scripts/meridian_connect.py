#!/usr/bin/env python3
"""meridian-connect — Meridian session hooks installer (pure stdlib).

Usage:
  python meridian_connect.py [--url URL] [--token TOKEN] [--project-id ID]
  ./meridian-connect [--url URL] [--token TOKEN] [--project-id ID]

Installs Claude Code, Codex, and Cursor integrations. No jq, no third-party
deps — works on any machine with Python 3.8+ (or as a PyInstaller binary with
no Python required at all).
"""
import argparse
import json
import os
import platform
import re
import shutil
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

DEFAULT_URL = "https://usemeridian.us"


def _http(method: str, url: str, *, token: str = "", body=None, timeout: int = 10):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "User-Agent": "meridian-connect/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        return None
    except Exception:
        return None


def _settings_path() -> Path:
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        return Path(appdata) / "Claude" / "settings.json"
    return Path.home() / ".claude" / "settings.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Meridian session hooks installer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--url", default="", help=f"Meridian server URL (default: {DEFAULT_URL})")
    parser.add_argument("--token", default="", help="Bearer token (skips browser auth)")
    parser.add_argument("--project-id", default="", help="Project ID (optional)")
    args = parser.parse_args()

    print()
    print("Meridian Connect")
    print("-----------------------")
    print()

    # ---- Step 1: URL ---------------------------------------------------------
    meridian_url = args.url.strip().rstrip("/")
    if not meridian_url:
        if sys.stdin.isatty():
            val = input(f"Meridian server URL [{DEFAULT_URL}]: ").strip()
            meridian_url = val or DEFAULT_URL
        else:
            meridian_url = DEFAULT_URL

    if not re.match(r"^https?://", meridian_url):
        print("Error: URL must start with https:// or http://", file=sys.stderr)
        return 1

    print(f"Checking {meridian_url} ...")
    health = _http("GET", f"{meridian_url}/health", timeout=5)
    if health is None:
        print(f"Error: Cannot reach {meridian_url}/health — is the server running?", file=sys.stderr)
        return 1
    print("  OK server is reachable")

    # ---- Step 2: Auth --------------------------------------------------------
    is_local = bool(re.match(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?(/|$)", meridian_url))
    token = args.token.strip()

    if not is_local:
        if not token:
            print()
            print("Opening browser to authenticate...")
            auth_url = f"{meridian_url}/auth/install"
            try:
                webbrowser.open(auth_url)
            except Exception:
                pass
            print(f"  Visit: {auth_url}")
            print()
            if sys.stdin.isatty():
                import getpass
                token = getpass.getpass("Paste the token shown in your browser: ").strip()
            else:
                print("Error: token is required for hosted Meridian (pass --token).", file=sys.stderr)
                return 1

        token = token.replace(" ", "")
        if not token:
            print("Error: token is required for hosted Meridian.", file=sys.stderr)
            return 1

        print()
        print("Validating token...")
        me = _http("GET", f"{meridian_url}/auth/me", token=token)
        if not me:
            print("Error: Token validation failed — is the token correct?", file=sys.stderr)
            return 1
        email = me.get("email", "")
        print(f"  Authenticated as: {email}")
    else:
        print()
        print("Self-hosted / localhost detected — skipping auth.")

    # ---- Step 3: Generate permanent token ------------------------------------
    if not is_local and token:
        perm = _http("POST", f"{meridian_url}/auth/tokens", token=token,
                     body={"label": "hooks-installer"})
        if perm and perm.get("token"):
            token = perm["token"]
            print("  Permanent token created.")

    # ---- Step 4: Build hook commands (cwd + hostname read at fire time) ------
    auth_header = f" -H 'Authorization: Bearer {token}'" if token else ""
    start_cmd = (
        f"curl -s -X POST{auth_header} -H 'Content-Type: application/json'"
        f" -d \"{{\\\"cwd\\\":\\\"$PWD\\\",\\\"hostname\\\":\\\"$(hostname)\\\"}}\""
        f" '{meridian_url}/hooks/session-start'"
        f" | jq -r '.hookSpecificOutput.additionalContext // empty' 2>/dev/null"
    )
    stop_cmd = (
        f"curl -s -X POST{auth_header} -H 'Content-Type: application/json'"
        f" -d \"{{\\\"hostname\\\":\\\"$(hostname)\\\"}}\""
        f" '{meridian_url}/hooks/stop' >/dev/null 2>&1"
    )

    if is_local:
        start_cmd = (
            f"curl -sf --max-time 3 '{meridian_url}/health' >/dev/null 2>&1 ||"
            f" {{ [ -f \"$HOME/pixi.toml\" ] && (cd \"$HOME\" && nohup pixi run start"
            f" >/dev/null 2>&1 &) && sleep 3; }}; {start_cmd}"
        )

    # ---- Step 5: Claude Code -------------------------------------------------
    settings_path = _settings_path()
    claude_detected = shutil.which("claude") is not None or settings_path.exists()

    if claude_detected:
        print()
        print(f"Claude Code detected — writing hooks to {settings_path}")
        settings_path.parent.mkdir(parents=True, exist_ok=True)

        existing: dict = {}
        if settings_path.exists():
            try:
                existing = json.loads(settings_path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}

        hooks = existing.setdefault("hooks", {})
        hooks["SessionStart"] = [{"matcher": "", "hooks": [{"type": "command", "command": start_cmd}]}]
        hooks["Stop"] = [{"matcher": "", "hooks": [{"type": "command", "command": stop_cmd}]}]
        settings_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        print("  OK SessionStart + Stop hooks written")

    # ---- Step 6: Codex -------------------------------------------------------
    codex_dir = Path.home() / ".codex"
    codex_detected = shutil.which("codex") is not None or codex_dir.exists()

    if codex_detected:
        print()
        print("Codex detected — writing MCP config to ~/.codex/config.toml")
        codex_dir.mkdir(parents=True, exist_ok=True)
        codex_config = codex_dir / "config.toml"

        auth_line = f'\napi_key = "{token}"' if token else ""
        # Escape toml strings: backslash + double-quote inside double-quoted strings
        def _toml_str(s: str) -> str:
            escaped = s.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'

        new_block = (
            f"\n[mcp_servers.meridian]\n"
            f'type = "http"\n'
            f'url = "{meridian_url}/mcp"{auth_line}\n'
            f"\n[hooks]\n"
            f"session_start = {_toml_str(start_cmd)}\n"
            f"stop = {_toml_str(stop_cmd)}\n"
        )

        if codex_config.exists():
            text = codex_config.read_text(encoding="utf-8")
            import re as _re
            text = _re.sub(r"\[mcp_servers\.meridian\].*?(?=\n\[|\Z)", "", text, flags=_re.DOTALL)
            text = _re.sub(r"\[hooks\].*?(?=\n\[|\Z)", "", text, flags=_re.DOTALL)
            codex_config.write_text(text.rstrip() + new_block, encoding="utf-8")
        else:
            codex_config.write_text(new_block.lstrip(), encoding="utf-8")
        print(f"  OK MCP config written to {codex_config}")

    # ---- Step 7: Cursor ------------------------------------------------------
    cursor_home = Path.home() / ".cursor"
    cursor_detected = shutil.which("cursor") is not None or cursor_home.exists()

    if cursor_detected:
        print()
        print("Cursor detected — writing .cursor/mcp.json in current directory")
        cursor_dir = Path.cwd() / ".cursor"
        cursor_dir.mkdir(parents=True, exist_ok=True)
        mcp_entry: dict = {"url": f"{meridian_url}/mcp"}
        if token:
            mcp_entry["headers"] = {"Authorization": f"Bearer {token}"}
        cursor_json = {"mcpServers": {"meridian": mcp_entry}}
        (cursor_dir / "mcp.json").write_text(json.dumps(cursor_json, indent=2), encoding="utf-8")
        print("  OK .cursor/mcp.json written")
        print("  Note: Cursor MCP tools available. Automatic session tracking requires Claude Code or Codex.")

    # ---- Step 8: Smoke test --------------------------------------------------
    print()
    print("Testing hook...")
    hostname = socket.gethostname()
    test_body = {"cwd": str(Path.cwd()), "hostname": hostname}
    result = _http("POST", f"{meridian_url}/hooks/session-start", token=token, body=test_body)
    if result is not None:
        print("  OK hook responded successfully")
    else:
        print("  WARNING: hook test failed (hooks still installed)")

    # ---- Done ----------------------------------------------------------------
    print()
    print("Done. Hooks installed. Restart Claude Code to activate.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
