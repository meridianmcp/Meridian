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
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

DEFAULT_URL = "https://usemeridian.us"

# 9784f8ef — env var names that may carry the API token, highest precedence
# first (the --token flag beats all of them). MERIDIAN_TOKEN is canonical;
# MERIDIAN_API_KEY / BEARER_TOKEN are legacy aliases. This mirrors
# meridian.tunnel_client.TOKEN_ENV_VARS -- duplicated on purpose because this
# script is dependency-free (PyInstaller entry point, no ``meridian`` import);
# tests/test_9784f8ef_install_token_exposure.py pins the two together.
TOKEN_ENV_VARS = ("MERIDIAN_TOKEN", "MERIDIAN_API_KEY", "BEARER_TOKEN")


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


def _token_from_env() -> str:
    """Return the API token from the environment, or "".

    9784f8ef — an env var is the preferred way to hand this script a token: a
    ``--token`` value sits on the command line, visible to every other process
    in a process listing. Precedence follows ``TOKEN_ENV_VARS``; the first
    non-blank value wins and a pasted ``Bearer `` prefix is stripped.
    """
    for name in TOKEN_ENV_VARS:
        candidate = (os.environ.get(name) or "").strip()
        if candidate:
            if candidate.lower().startswith("bearer "):
                candidate = candidate[7:].strip()
            if candidate:
                return candidate
    return ""


def _windows_principal() -> str:
    """``DOMAIN\\user`` for the current Windows account (for icacls grants)."""
    import getpass

    user = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN", "")
    return f"{domain}\\{user}" if domain else user


def _restrict_to_owner(path) -> bool:
    """Restrict ``path`` to the current user only. True when the change applied.

    9784f8ef — ``os.chmod(path, 0o600)`` is a no-op on Windows (it only toggles
    the read-only bit), so the old "restrictive permissions" on hook_auth.conf
    were never real there. On Windows this drops inherited ACEs and grants only
    the current user via ``icacls``; on POSIX it is chmod 600 (700 for a
    directory). Never raises: a missing ``icacls``, a non-zero exit or an OS
    error returns False so the caller can warn instead of crashing the install.
    """
    path = Path(path)
    is_dir = path.is_dir()
    if platform.system() == "Windows":
        icacls = shutil.which("icacls")
        if not icacls:
            return False
        principal = _windows_principal()
        grant = f"{principal}:(OI)(CI)F" if is_dir else f"{principal}:F"
        try:
            proc = subprocess.run(
                [icacls, str(path), "/inheritance:r", "/grant:r", grant],
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0
    try:
        os.chmod(path, 0o700 if is_dir else 0o600)
    except OSError:
        return False
    return True


def _write_private_file(path, text: str) -> bool:
    """Write ``text`` to ``path`` without ever exposing it in a shared file.

    The file is created (empty) and locked down to the current user BEFORE the
    secret is written, so there is no window in which the token sits in a file
    other local users can read. Returns whether owner-only permissions were
    applied; raises OSError only when the write itself fails.
    """
    path = Path(path)
    if platform.system() == "Windows":
        path.write_text("", encoding="utf-8")
        hardened = _restrict_to_owner(path)
        path.write_text(text, encoding="utf-8")
        return hardened
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    # os.open's mode only applies at creation; tighten a pre-existing file too.
    return _restrict_to_owner(path)


def _write_curl_header_config(token: str) -> str:
    """Write a local curl `-K` config file holding the Authorization header.

    ba31dedf — returns a path curl can read the header from at hook-fire
    time, so the raw token is never a literal substring of the hook command
    string this script writes into settings.json, and never appears in this
    process's argv on every SessionStart/Stop firing (see call site comment).
    Returns "" when there is no token (self-hosted/local, no auth needed) --
    callers must treat an empty return as "omit the auth flag entirely",
    never as a config file with an empty header.

    9784f8ef — the file is owner-only on every platform (icacls on Windows,
    where the previous chmod was a silent no-op). If hardening could not be
    applied the file is still written (auth would otherwise silently vanish
    from the hooks) but a warning naming the path is printed to stderr.
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
        hardened = _write_private_file(
            cfg_path, f'header = "Authorization: Bearer {token}"\n'
        )
    except OSError:
        return ""
    if not hardened:
        print(
            f"  WARNING: could not restrict {cfg_path} to your user account; "
            "check its permissions (it holds your Meridian token).",
            file=sys.stderr,
        )
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


# ---------------------------------------------------------------------------
# bb307c27 -- Codex / Cursor config writers that MERGE instead of clobbering.
#
# The previous writers (a) regex-deleted the user's whole ``[hooks]`` table in
# ~/.codex/config.toml, (b) wrote ``type = "http"`` + ``api_key`` -- neither is a
# documented Codex MCP field -- and (c) overwrote ``.cursor/mcp.json`` in the
# CURRENT folder with a plaintext token, dropping any other server and possibly
# landing the token in a git repo. Only stdlib is available here (no TOML
# writer, tomllib needs 3.11), so the Codex writer uses a small statement-aware
# TOML scanner and edits only Meridian-owned sections/keys line by line.
# ---------------------------------------------------------------------------

# Comment line written above the table we own; removed together with it.
_MERIDIAN_TOML_MARKER = "# Meridian - managed by meridian-connect (safe to remove)"
_LEGACY_TOML_MARKERS = (_MERIDIAN_TOML_MARKER, "# Meridian - added by hooks.ps1")
# Substring identifying a [hooks] value as Meridian's own (each command embeds
# the hook URL). A same-named key WITHOUT it is the user's and is left alone.
_CODEX_HOOK_OWNERSHIP = {"session_start": "/hooks/session-start", "stop": "/hooks/stop"}


def _toml_str(value: str) -> str:
    """A TOML basic string. JSON string escapes are a valid subset of TOML's."""
    return json.dumps(value)


def _toml_split_key(text: str) -> tuple:
    """Split a (possibly dotted, possibly quoted) TOML key into its parts."""
    parts, buf, quote, i, n = [], [], None, 0, len(text)
    while i < n:
        c = text[i]
        if quote:
            if c == "\\" and quote == '"' and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            else:
                buf.append(c)
        elif c in "\"'":
            quote = c
        elif c == ".":
            parts.append("".join(buf))
            buf = []
        elif not c.isspace():
            buf.append(c)
        i += 1
    parts.append("".join(buf))
    return tuple(parts)


def _toml_find_eq(line: str) -> int:
    """Index of the first ``=`` outside a quoted key, or -1."""
    quote = None
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "=":
            return i
        elif c == "#":
            return -1
        i += 1
    return -1


def _toml_header(stripped: str):
    """Parse ``[a.b]`` / ``[[a.b]]`` -> ``(is_array_table, "a.b")`` or None."""
    is_array = stripped.startswith("[[")
    start = 2 if is_array else 1
    close = "]]" if is_array else "]"
    quote = None
    j, n = start, len(stripped)
    while j < n:
        c = stripped[j]
        if quote:
            if c == "\\" and quote == '"':
                j += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif stripped.startswith(close, j):
            rest = stripped[j + len(close):].strip()
            if rest and not rest.startswith("#"):
                return None
            return is_array, stripped[start:j]
        j += 1
    return None


def _toml_scan_value(seg: str, ml, depth: int):
    """Advance the (multi-line string, bracket depth) state across ``seg``."""
    i, n = 0, len(seg)
    while i < n:
        if ml:
            if ml == '"""':
                j = i
                while j < n and not seg.startswith('"""', j):
                    j += 2 if seg[j] == "\\" else 1
                if j >= n:
                    return ml, depth
                i, ml = j + 3, None
            else:
                j = seg.find("'''", i)
                if j < 0:
                    return ml, depth
                i, ml = j + 3, None
            continue
        c = seg[i]
        if c == "#":
            break
        if seg.startswith('"""', i):
            ml, i = '"""', i + 3
        elif seg.startswith("'''", i):
            ml, i = "'''", i + 3
        elif c == '"':
            i += 1
            while i < n and seg[i] != '"':
                i += 2 if seg[i] == "\\" else 1
            i += 1
        elif c == "'":
            j = seg.find("'", i + 1)
            i = j + 1 if j >= 0 else n
        else:
            if c in "[{":
                depth += 1
            elif c in "]}":
                depth = max(0, depth - 1)
            i += 1
    return ml, depth


def _toml_layout(lines):
    """Statement layout of a TOML document (no values are interpreted).

    Returns ``(headers, kvs)``: ``headers`` = ``[(line, is_array, key_tuple)]``;
    ``kvs`` = ``[(first_line, last_line, key_tuple, owner)]`` where ``owner`` is
    the index into ``headers`` of the table the key belongs to (-1 = root).
    Multi-line strings and arrays are tracked, so a line inside them that merely
    LOOKS like a table header is never mistaken for one.
    """
    headers, kvs = [], []
    ml, depth, start, key = None, 0, 0, ()
    for idx, raw in enumerate(lines):
        if ml is None and depth == 0:
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("["):
                parsed = _toml_header(stripped)
                if parsed is not None:
                    headers.append((idx, parsed[0], _toml_split_key(parsed[1])))
                    continue
            eq = _toml_find_eq(raw)
            key = _toml_split_key(raw[:eq]) if eq >= 0 else ()
            start = idx
            ml, depth = _toml_scan_value(raw[eq + 1:] if eq >= 0 else "", None, 0)
        else:
            ml, depth = _toml_scan_value(raw, ml, depth)
        if ml is None and depth == 0:
            kvs.append((start, idx, key, len(headers) - 1))
    return headers, kvs


def _toml_section_extent(lines, headers, kvs, pos: int):
    """``(first, last)`` line of the table at ``headers[pos]`` (inclusive).

    ``last`` is the end of its final key/value, so comments the user wrote
    above the NEXT table are never swallowed.
    """
    first = headers[pos][0]
    last = first
    for kv_start, kv_end, _key, owner in kvs:
        if owner == pos and kv_end > last:
            last = kv_end
    return first, last


def _drop_lines(lines, first: int, last: int) -> None:
    """Delete ``lines[first..last]`` in place, plus our marker comment right
    above it, and avoid leaving a doubled blank line behind."""
    while first > 0 and lines[first - 1].strip() in _LEGACY_TOML_MARKERS:
        first -= 1
    del lines[first:last + 1]
    while 0 < first < len(lines) and not lines[first - 1].strip() and not lines[first].strip():
        del lines[first]


def _codex_conflicts(headers, kvs):
    """Forms of the ``meridian`` server / ``hooks`` table this scanner cannot
    edit safely (inline tables, dotted keys at the root)."""
    found = set()
    for _s, _e, key, owner in kvs:
        if owner == -1 and key and key[0] in ("mcp_servers", "hooks"):
            found.add(key[0])
        if owner >= 0 and headers[owner][2] == ("mcp_servers",) and key[:1] == ("meridian",):
            found.add("mcp_servers")
    return found


def _merge_codex_config(text: str, meridian_url: str, token: str, start_cmd: str, stop_cmd: str):
    """Return ``(new_text, notes)`` -- ``text`` with Meridian's Codex config
    merged in and EVERYTHING else (the user's ``[hooks]`` keys, other MCP
    servers, comments) left byte-for-byte intact.

    * ``[mcp_servers.meridian]`` (and sub-tables) are ours: replaced.
    * ``[hooks]``: only the ``session_start`` / ``stop`` keys are touched, and
      only when absent or already Meridian's own. A same-named key the user
      wrote is never overwritten.
    * MCP auth uses ``http_headers`` -- documented by Codex for streamable-HTTP
      servers, unlike the ``type`` / ``api_key`` fields written before.
    """
    notes = []
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    headers, kvs = _toml_layout(lines)
    conflicts = _codex_conflicts(headers, kvs)

    write_mcp = "mcp_servers" not in conflicts
    write_hooks = "hooks" not in conflicts
    if not write_mcp:
        notes.append(
            "WARNING: the existing config defines mcp_servers.meridian in a form this "
            "installer cannot edit safely (inline table / dotted key); left untouched. "
            f"Add it by hand: [mcp_servers.meridian] url = {_toml_str(meridian_url + '/mcp')}"
            + (" and an http_headers Authorization header." if token else ".")
        )
    if not write_hooks:
        notes.append(
            "WARNING: the existing config defines hooks in a form this installer cannot "
            "edit safely (inline table / dotted key); Meridian hooks not added."
        )

    # 1. drop the Meridian-owned MCP tables (bottom-up so indexes stay valid)
    if write_mcp:
        for pos in range(len(headers) - 1, -1, -1):
            if headers[pos][2][:2] == ("mcp_servers", "meridian"):
                first, last = _toml_section_extent(lines, headers, kvs, pos)
                _drop_lines(lines, first, last)

    # 2. upsert only Meridian's own keys inside [hooks]
    if write_hooks:
        for key, command in (("session_start", start_cmd), ("stop", stop_cmd)):
            headers, kvs = _toml_layout(lines)
            new_line = f"{key} = {_toml_str(command)}"
            hooks_pos = next(
                (p for p, h in enumerate(headers) if h[2] == ("hooks",) and not h[1]), None
            )
            if hooks_pos is None:
                if lines and lines[-1].strip():
                    lines.append("")
                lines.extend(["[hooks]", new_line])
                continue
            existing = [kv for kv in kvs if kv[3] == hooks_pos and kv[2] == (key,)]
            if existing:
                first, last, _k, _o = existing[0]
                old = "\n".join(lines[first:last + 1])
                if _CODEX_HOOK_OWNERSHIP[key] in old:
                    lines[first:last + 1] = [new_line]
                else:
                    notes.append(
                        f"WARNING: [hooks] already has your own '{key}' entry; left it "
                        "untouched (Meridian's hook was NOT added for that event)."
                    )
            else:
                _first, last = _toml_section_extent(lines, headers, kvs, hooks_pos)
                lines.insert(last + 1, new_line)

    # 3. append the Meridian MCP table
    if write_mcp:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("")
        lines.append(_MERIDIAN_TOML_MARKER)
        lines.append("[mcp_servers.meridian]")
        lines.append(f"url = {_toml_str(meridian_url + '/mcp')}")
        if token:
            lines.append(f"http_headers = {{ Authorization = {_toml_str('Bearer ' + token)} }}")

    while lines and not lines[-1].strip():
        lines.pop()
    return newline.join(lines) + newline, notes


def _backup_once(path: Path) -> None:
    """Copy ``path`` to ``<name>.meridian-bak`` unless a backup already exists,
    so the first backup stays the user's pristine original across re-runs."""
    backup = path.with_name(path.name + ".meridian-bak")
    if path.exists() and not backup.exists():
        try:
            shutil.copy2(path, backup)
        except OSError:
            pass


def _write_text_atomic(path: Path, text: str) -> None:
    """Write via a sibling temp file + rename, so a crash never leaves the user's
    config half-written. ``newline=""`` keeps the line endings ``text`` carries."""
    tmp = path.with_name(path.name + ".meridian-tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _configure_codex(config_path: Path, meridian_url: str, token: str, start_cmd: str, stop_cmd: str):
    """Merge Meridian into ``config_path``. Returns ``(ok, notes)``; never
    raises on an unreadable/locked file -- it reports and leaves it alone."""
    notes = []
    existing = ""
    if config_path.exists():
        try:
            existing = config_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            return False, [f"WARNING: could not read {config_path} ({exc}); left untouched."]
    new_text, merge_notes = _merge_codex_config(existing, meridian_url, token, start_cmd, stop_cmd)
    notes.extend(merge_notes)
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        if existing:
            _backup_once(config_path)
        _write_text_atomic(config_path, new_text)
    except OSError as exc:
        return False, notes + [f"WARNING: could not write {config_path} ({exc})."]
    if token and not _restrict_to_owner(config_path):
        notes.append(f"WARNING: could not restrict {config_path} to your user account; it now holds your token.")
    notes.append(f"OK Meridian MCP server merged into {config_path} (your other settings were preserved)")
    return True, notes


def _cursor_config_path() -> Path:
    """Cursor's USER-GLOBAL MCP config (documented: ``~/.cursor/mcp.json``).

    Deliberately not ``<cwd>/.cursor/mcp.json``: that is a project file that is
    routinely committed, and the installer can be run from any folder.
    """
    return Path.home() / ".cursor" / "mcp.json"


def _configure_cursor(meridian_url: str, token: str):
    """Merge the ``meridian`` server into Cursor's global mcp.json, leaving every
    other server and key intact. Returns ``(ok, message)``."""
    path = _cursor_config_path()
    entry: dict = {"url": f"{meridian_url}/mcp"}
    if token:
        entry["headers"] = {"Authorization": f"Bearer {token}"}
    data: dict = {}
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            return False, f"WARNING: could not read {path} ({exc}); left untouched."
        if raw.strip():
            try:
                data = json.loads(raw)
            except ValueError:
                return False, (
                    f"WARNING: {path} is not plain JSON (comments?); left untouched. "
                    f"Add this server by hand under \"mcpServers\": \"meridian\" with url "
                    f"{meridian_url}/mcp" + (" and an Authorization header." if token else ".")
                )
    if not isinstance(data, dict):
        return False, f"WARNING: {path} does not contain a JSON object; left untouched."
    servers = data.get("mcpServers")
    if servers is None:
        servers = data["mcpServers"] = {}
    if not isinstance(servers, dict):
        return False, f"WARNING: 'mcpServers' in {path} is not an object; left untouched."
    servers["meridian"] = entry
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _backup_once(path)
        _write_text_atomic(path, json.dumps(data, indent=2) + "\n")
    except OSError as exc:
        return False, f"WARNING: could not write {path} ({exc})."
    if token and not _restrict_to_owner(path):
        return True, f"OK merged into {path} (WARNING: could not restrict it to your user account)"
    return True, f"OK merged into {path} (your other MCP servers were preserved)"


def _legacy_cursor_project_token_warning(cwd: Path) -> str:
    """Earlier versions wrote ``<cwd>/.cursor/mcp.json`` with the raw token. If
    such a file is still here, say so (read-only check; never modifies it)."""
    legacy = cwd / ".cursor" / "mcp.json"
    try:
        if legacy.is_file() and "Bearer sk_meridian_" in legacy.read_text(encoding="utf-8-sig"):
            return (
                f"WARNING: {legacy} holds a Meridian token in plaintext (written by an older "
                "installer). Delete that entry, make sure it is not committed to git, and "
                "consider rotating the token."
            )
    except (OSError, UnicodeDecodeError):
        pass
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Meridian session hooks installer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--url", default="", help=f"Meridian server URL (default: {DEFAULT_URL})")
    parser.add_argument(
        "--token",
        default="",
        help=(
            "Bearer token (skips browser auth). Prefer setting MERIDIAN_TOKEN "
            "(or the legacy MERIDIAN_API_KEY / BEARER_TOKEN) in the "
            "environment instead: a --token value is visible to other "
            "processes in a process listing."
        ),
    )
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
    if not token and not is_local:
        # 9784f8ef — env-var hand-off (install.ps1 sets MERIDIAN_TOKEN for this
        # process only) so the token never has to ride on the command line.
        # Hosted only: a hosted token must not be forwarded to a local server.
        token = _token_from_env()

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
                print(
                    "Error: token is required for hosted Meridian "
                    "(set MERIDIAN_TOKEN, or pass --token).",
                    file=sys.stderr,
                )
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
        print("Codex detected -- merging Meridian into ~/.codex/config.toml")
        # bb307c27 -- merge, never regex-delete: the user's own [hooks] keys and
        # every other table survive; only Meridian-owned sections/keys change.
        _codex_ok, codex_notes = _configure_codex(
            codex_dir / "config.toml", meridian_url, token, start_cmd, stop_cmd
        )
        for note in codex_notes:
            print(f"  {note}")

    # ---- Step 7: Cursor ------------------------------------------------------
    cursor_home = Path.home() / ".cursor"
    cursor_detected = shutil.which("cursor") is not None or cursor_home.exists()

    if cursor_detected:
        print()
        print("Cursor detected -- merging Meridian into ~/.cursor/mcp.json (user-global)")
        # bb307c27 -- the user-global file, merged: not <cwd>/.cursor/mcp.json,
        # which was overwritten wholesale, could land in a git repo, and held
        # the raw token.
        _cursor_ok, cursor_msg = _configure_cursor(meridian_url, token)
        print(f"  {cursor_msg}")
        legacy_warning = _legacy_cursor_project_token_warning(Path.cwd())
        if legacy_warning:
            print(f"  {legacy_warning}")
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
