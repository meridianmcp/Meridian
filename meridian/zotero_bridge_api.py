"""The Zotero Bridge plugin's local write API, on the Meridian side: the token file and the profile prefs that point the plugin at it.

The plugin listens on the loopback address only. It accepts a request only when the bearer token in the token file
matches. This module creates that file once, and keeps a usable token on every later install, so a reinstall never
breaks a client that already holds the token. Nothing here returns or prints the token value; callers see only status.

user.js is read and written as bytes. latin-1 maps each byte to exactly one character, so every line this module does
not manage comes back byte for byte, whatever its encoding, and a CRLF file stays CRLF on every platform.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

from meridian.local_runner import default_state_dir

API_PORT = 23125
TOKEN_PREFIX = "sk_meridian_"
TOKEN_FILENAME = "api_token"
BRIDGE_SUBDIR = "zotero_bridge"
PREF_PREFIX = "extensions.zotero.meridianZoteroBridge."
PORT_PREF = PREF_PREFIX + "apiPort"
TOKEN_PATH_PREF = PREF_PREFIX + "apiTokenPath"
USER_JS = "user.js"
TOKEN_CREATED = "created"
TOKEN_KEPT = "kept"
PREFS_ADDED = "added"
PREFS_UPDATED = "updated"
PREFS_PRESENT = "present"

# Matches the plugin's own check in lib/api/auth.js: a random URL-safe value of a useful length.
_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_-]{32,}$")
# Whitespace and byte-order marks (U+FEFF) that a token file may carry at either end.
_TOKEN_TRIM = " \t\r\n\f\v﻿"
# ASCII flag: \s must not match a latin-1 byte such as 0xA0 or 0x85 that appears in user.js.
_MANAGED_LINE = re.compile(
    r"^\s*user_pref\(\s*\"(?:" + re.escape(PORT_PREF) + "|" + re.escape(TOKEN_PATH_PREF) + r")\"\s*,.*\)\s*;?\s*$",
    re.ASCII,
)


def token_file_path() -> Path:
    """The token file: a folder next to the plugin's signing record, under Meridian's local-runner state folder."""
    return default_state_dir() / BRIDGE_SUBDIR / TOKEN_FILENAME


def _usable(text: str) -> bool:
    """True when ``text``, without surrounding whitespace or byte-order marks, has the plugin's token shape."""
    return bool(_TOKEN_SHAPE.match(text.strip(_TOKEN_TRIM)))


def _write_private(target: Path, payload: bytes) -> None:
    """Write through a temporary file and replace the target, so a crash leaves the old file or the new one, never half.

    Binary mode, so Windows never turns an LF into CRLF. The temporary file is created 0600 on POSIX.
    """
    temp = target.with_name(target.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
    os.replace(temp, target)


def token_is_usable(path: "Path | str") -> bool:
    """True when the token file exists and holds a usable token. Reads only. Never raises.

    The file is read as UTF-8 with a leading byte-order mark dropped. A file that does not decode (UTF-16, for example)
    is unusable: a UnicodeDecodeError is a ValueError, and it is caught here.
    """
    target = Path(path)
    try:
        return target.is_file() and _usable(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False


def _restrict_to_owner(target: Path) -> None:
    """On Windows, leave the token readable by its owner only.

    The inherited ACL otherwise gives other accounts read access, sandbox groups among them. POSIX already has 0600.
    This is the same owner-only grant that scripts/install_tunnel.ps1 applies to Meridian's own tunnel files. Failure
    raises, so an install never reports success over a file other accounts can read.
    """
    if sys.platform != "win32":
        return
    user = getpass.getuser()
    result = subprocess.run(
        ["icacls", str(target), "/inheritance:r", "/grant:r", f"{user}:F"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise OSError(f"Could not restrict the bridge token file to {user}: {detail}")


def ensure_api_token(path: "Path | str") -> str:
    """Create the token file unless it already holds a usable token, which is kept as it is.

    Returns ``created`` or ``kept``. An unusable file, including one that does not decode, is replaced. The token
    value is never returned. On Windows the file is restricted to its owner on every call, so a rerun repairs the ACL.
    """
    target = Path(path)
    if token_is_usable(target):
        _restrict_to_owner(target)
        return TOKEN_KEPT
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_private(target, (token + "\n").encode("ascii"))
    _restrict_to_owner(target)
    return TOKEN_CREATED


def api_pref_lines(token_path: "Path | str", port: int = API_PORT) -> list[str]:
    """The two managed ``user_pref`` lines. JSON string escaping gives a value Firefox's prefs parser reads back exactly."""
    return [
        f'user_pref("{PORT_PREF}", {int(port)});',
        f'user_pref("{TOKEN_PATH_PREF}", {json.dumps(str(token_path))});',
    ]


def read_user_js(profile: "Path | str") -> str:
    """user.js as latin-1 text: one character per byte, so writing it back as latin-1 restores the same bytes.

    Empty when the profile has no user.js. Raises OSError when the file exists and cannot be read.
    """
    path = Path(profile) / USER_JS
    if not path.exists():
        return ""
    return path.read_bytes().decode("latin-1")


def write_user_js(profile: "Path | str", text: str) -> None:
    """Replace user.js with ``text``, written as latin-1 bytes through a temporary file."""
    _write_private(Path(profile) / USER_JS, text.encode("latin-1"))


def user_js_lines(text: str) -> "tuple[list[str], str]":
    """The lines of user.js without their LF, and the file's line ending (CRLF when the bytes contain one, else LF).

    Splits on LF only. A latin-1 byte such as 0x85 or 0x0C is not a line break here, as it would be for
    str.splitlines(). A CR before the LF stays on its line, so every kept line keeps its own bytes. An unterminated last
    line is completed with the file's line ending, so a line added after it starts on a line of its own.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    if text and not text.endswith("\n"):
        text += newline
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines, newline


def ensure_api_prefs(profile: "Path | str", token_path: "Path | str", port: int = API_PORT) -> str:
    """Set the two prefs in the profile's user.js and leave every other line's bytes as they are.

    Returns ``added`` (no managed line existed), ``updated`` (a managed line changed), or ``present`` (already right,
    nothing written). New lines use the file's own line ending, so a CRLF file stays CRLF.
    """
    lines, newline = user_js_lines(read_user_js(profile))
    desired = api_pref_lines(token_path, port)
    existing = [line.strip() for line in lines if _MANAGED_LINE.match(line)]
    if sorted(existing) == sorted(desired):
        return PREFS_PRESENT
    kept = [line for line in lines if not _MANAGED_LINE.match(line)]
    body = "".join(line + "\n" for line in kept) + "".join(line + newline for line in desired)
    write_user_js(profile, body)
    return PREFS_UPDATED if existing else PREFS_ADDED
