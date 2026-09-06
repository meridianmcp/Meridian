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


def _write_curl_header_config(token: str) -> str:
    """Write a local curl `-K` config file holding the Authorization header.

    ba31dedf — returns a path curl can read the header from at hook-fire
    time, so the raw token is never a literal substring of the hook command
    string this script writes into settings.json, and never appears in this
    process's argv on every SessionStart/Stop firing (see call site comment).
    Returns "" when there is no token (self-hosted/local, no auth needed) --
    callers must treat an empty return as "omit the auth flag entirely",
    never as a config file with an empty header.
    """
    if not token:
        return ""
    cfg_dir = Path.home() / ".meridian"
    try:
        cfg_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return ""
    cfg_path = cfg_dir / "hook_auth.conf"
    try:
        # curl -K config-file syntax: one `option = "value"` per line.
        cfg_path.write_text(
            f'header = "Authorization: Bearer {token}"\n', encoding="utf-8"
        )
        try:
            os.chmod(cfg_path, 0o600)  # best-effort on POSIX; no-op on Windows
        except OSError:
            pass
    except OSError:
        return ""
    return str(cfg_path)


def _local_repo_hint() -> str:
    """Directory to try starting the local server from (is_local health-check
    fallback). NEVER guesses ``$HOME`` (ba31dedf) -- an unset
    ``MERIDIAN_LOCAL_REPO``, or one that itself resolves to a bare home
    directory, means "don't guess, skip the fallback" rather than silently
    operating over the user's entire home tree.
    """
    hint = os.environ.get("MERIDIAN_LOCAL_REPO", "").strip()
    if not hint:
        return ""
    resolved = str(Path(hint).expanduser().resolve())
    home = str(Path.home().resolve())
    if resolved.rstrip("\\/") == home.rstrip("\\/"):
        return ""
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Meridian session hooks installer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--url", default="", help=f"Meridian server URL (default: {DEFAULT_URL})")
    parser.add_argument("--token", default="", help="Bearer token (skips browser auth)")
    parser.add_argument("--project-id", default="", help="Project ID (optional)")
    parser.add_argument(
        "--tunnel",
        action="store_true",
        help=(
            "After hooks are installed, start the filesystem tunnel (Pro). "
            "Prints your permanent MCP URL and keeps running — add it to "
            "claude.ai once and never touch it again."
        ),
    )
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
    # ba31dedf — the token must NEVER be a literal substring of the hook
    # command string written into settings.json: these hooks fire on EVERY
    # SessionStart/Stop, so a literal `-H 'Authorization: Bearer <token>'`
    # here would (a) sit in settings.json in plaintext — a file people
    # routinely paste into bug reports / dotfile-sync repos — and (b) put the
    # raw token in this process's argv on every single invocation, visible to
    # `ps`/Task Manager to any other user on a shared or process-monitored
    # machine. curl's `-K <file>` reads headers from a local, restrictive-
    # permission config file instead of argv/settings.json — the standard
    # technique for keeping a secret out of both places. See
    # _write_curl_header_config below.
    auth_cfg_path = _write_curl_header_config(token)
    auth_flag = f" -K \"{auth_cfg_path}\"" if auth_cfg_path else ""
    start_cmd = (
        f"curl -s -X POST{auth_flag} -H 'Content-Type: application/json'"
        f" -d \"{{\\\"cwd\\\":\\\"$PWD\\\",\\\"hostname\\\":\\\"$(hostname)\\\"}}\""
        f" '{meridian_url}/hooks/session-start'"
        f" | jq -r '.hookSpecificOutput.additionalContext // empty' 2>/dev/null"
    )
    stop_cmd = (
        f"curl -s -X POST{auth_flag} -H 'Content-Type: application/json'"
        f" -d \"{{\\\"hostname\\\":\\\"$(hostname)\\\"}}\""
        f" '{meridian_url}/hooks/stop' >/dev/null 2>&1"
    )

    if is_local:
        # ba31dedf — never fall back to $HOME: the prior unconditional
        # `cd "$HOME" && nohup pixi run start` assumed the Meridian source
        # checkout lives directly in the user's home directory, which is both
        # fragile and the exact "home-directory execution fallback" class of
        # bug the repo-scope guard (meridian/repo_scope.py) exists to reject
        # elsewhere. This script is intentionally dependency-free (no
        # `meridian` package import — see module docstring), so the fix here
        # is self-contained: only attempt the fallback against an explicitly
        # configured local repo path, and refuse a bare home directory even
        # if one is configured. An unset/rejected hint means "don't guess" —
        # the fallback is skipped entirely rather than defaulting to $HOME.
        _local_repo = _local_repo_hint()
        if _local_repo:
            start_cmd = (
                f"curl -sf --max-time 3 '{meridian_url}/health' >/dev/null 2>&1 ||"
                f" {{ [ -f \"{_local_repo}/pixi.toml\" ] && (cd \"{_local_repo}\" && nohup pixi run start"
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

    # ---- Done / Tunnel -------------------------------------------------------
    print()
    if args.tunnel and not is_local:
        print("Hooks installed. Starting filesystem tunnel...")
        print()
        import asyncio
        import selectors
        from meridian.tunnel_client import run_tunnel

        if platform.system() == "Windows":
            # f73810d5/3ac13517 — WindowsSelectorEventLoopPolicy, NOT
            # DefaultEventLoopPolicy() (which on Windows is the ProactorEventLoopPolicy).
            # meridian-connect.exe is built from THIS script, so the wrong policy here
            # is exactly what shipped the live psycopg_pool.PoolTimeout tunnel-startup
            # failure: hand-setting one SelectorEventLoop leaves the policy on Proactor,
            # so psycopg's later loop derivation still gets an unsupported Proactor loop.
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            _loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
            asyncio.set_event_loop(_loop)
        else:
            _loop = asyncio.get_event_loop()

        try:
            return _loop.run_until_complete(
                run_tunnel(token=token, base_url=meridian_url)
            )
        except KeyboardInterrupt:
            print("\ntunnel: stopped", flush=True)
            return 0

    if args.tunnel and is_local:
        print("Note: --tunnel is for hosted Meridian (Pro). Skipping for local server.")
        print()

    print("Done. Hooks installed. Restart Claude Code to activate.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
