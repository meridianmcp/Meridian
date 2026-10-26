"""Zotero bridge: the open local writes setting, kept in a profile's user.js.

The bridge reads ``extensions.zotero.meridianZoteroBridge.localWritesOpen`` once, when Zotero starts. While it is
true, every write grant is given with no prompt at the desktop. The shipped plugin default (prefs.js) stays false,
and nothing here changes it. This module sets that one preference in a profile's ``user.js``, keeps every other
line of the file byte for byte, and can restart Zotero so the change takes effect.

Standard library only at import time. Zotero's profile lookup and the close and start helpers are imported inside
the functions that use them, so the installer can import this module without a cycle.

Command: ``python -m meridian zotero local-writes status|on|off [--profile PATH] [--restart] [--dry-run]``.
"""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

PREF = "extensions.zotero.meridianZoteroBridge.localWritesOpen"
MARK = (
    "// Meridian: local writes open (set by python -m meridian zotero local-writes). "
    "Off means the bridge asks at the desktop for each write."
)
USER_JS = "user.js"
PROFILE_MARKER = "prefs.js"

STATE_ON = "on"
STATE_OFF = "off"
STATE_UNSET = "unset"
STATE_UNKNOWN = "unknown"  # only the tray reports this, for a user.js that cannot be read

# The one form this module reads and writes: a whole line, with only spaces or tabs before and after the statement,
# that sets the pref to a boolean. Every line that names the pref must match it exactly, or the file is refused.
_STATEMENT = re.compile(
    r'[ \t]*user_pref[ \t]*\([ \t]*"' + re.escape(PREF) + r'"[ \t]*,[ \t]*(true|false)[ \t]*\)[ \t]*;[ \t\r]*',
    re.ASCII,
)
_REFUSED_TEXT = (
    "a line for the Zotero local writes setting is in a form Meridian cannot read safely. "
    "The setting must be one complete user_pref statement on its own line."
)

_STATE_TEXT = {
    STATE_ON: "on",
    STATE_OFF: "off",
    STATE_UNSET: "not set (off by default)",
    STATE_UNKNOWN: "unknown",
}


# ---------------------------------------------------------------------------
# Reading and planning (no writes)
# ---------------------------------------------------------------------------


def user_js_path(profile_dir: "Path | str") -> Path:
    """The profile's ``user.js``. The file may not exist yet."""
    return Path(profile_dir) / USER_JS


def state_text(state: str) -> str:
    """The words for a state, for the CLI and the tray. An unrecognised state reads as unknown."""
    return _STATE_TEXT.get(state, _STATE_TEXT[STATE_UNKNOWN])


def _state_for(enabled: bool) -> str:
    return STATE_ON if enabled else STATE_OFF


def _split_lines(text: str) -> "list[str]":
    """The lines of ``text``, each keeping its line feed. Only LF ends a line, so joining the lines gives ``text`` back."""
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def _statements(lines: "Iterable[str]") -> "list[tuple[int, bool]]":
    """The pref's statements in file order, as (line index, value). Raises ValueError for a bad line.

    Every line that names the pref must be exactly one complete statement. A split call, two statements on one line or
    any other syntax raises ValueError, so nothing is read from such a file and nothing is written to it.
    """
    found: "list[tuple[int, bool]]" = []
    for index, line in enumerate(lines):
        if PREF not in line:
            continue
        match = _STATEMENT.fullmatch(line.rstrip("\n"))
        if match is None:
            raise ValueError(_REFUSED_TEXT)
        found.append((index, match.group(1) == "true"))
    return found


def _state_of_statements(statements: "list[tuple[int, bool]]") -> str:
    """The state the last statement sets, as Zotero reads the file. Unset when there are none."""
    if not statements:
        return STATE_UNSET
    return STATE_ON if statements[-1][1] else STATE_OFF


def read_state(profile_dir: "Path | str") -> str:
    """``on``, ``off`` or ``unset`` for the profile's user.js. A missing file or folder reads as unset.

    The file is read as latin-1, the same bytes in and out, so a non-ASCII line is never misread. A line that names the
    pref in any form other than one complete statement raises ValueError, the same refusal the write path makes.
    Other errors (for example a file that cannot be read) are raised as OSError.
    """
    try:
        text = user_js_path(profile_dir).read_bytes().decode("latin-1")
    except (FileNotFoundError, NotADirectoryError):
        return STATE_UNSET
    return _state_of_statements(_statements(_split_lines(text)))


def _require_profile(profile_dir: "Path | str") -> Path:
    """The profile folder, or ValueError with the reason. Nothing is read or written here."""
    folder = Path(profile_dir)
    if not folder.is_dir():
        raise ValueError(f"No Zotero profile folder exists at {folder}. Nothing was changed.")
    if not (folder / PROFILE_MARKER).is_file():
        raise ValueError(f"{folder} is not a Zotero profile: it has no {PROFILE_MARKER}. Nothing was changed.")
    return folder


def _plan(profile_dir: "Path | str", enabled: bool) -> "tuple[str, bytes | None]":
    """The state before, and the new user.js bytes, or None when the file would not change.

    A file with any line that names the pref in a form other than one complete statement raises ValueError before
    anything is planned. The file would not change when the state already matches and exactly one statement sets the
    pref. Every other line is kept byte for byte. The statements and the MARK line this module wrote before are
    replaced, and the MARK and the pref line are appended at the end.
    """
    folder = _require_profile(profile_dir)
    try:
        original = (folder / USER_JS).read_bytes()
    except FileNotFoundError:
        original = b""
    text = original.decode("latin-1")
    lines = _split_lines(text)
    statements = _statements(lines)
    before = _state_of_statements(statements)
    if before == _state_for(enabled) and len(statements) == 1:
        return before, None
    newline = "\r\n" if "\r\n" in text else "\n"
    replaced = {index for index, _value in statements}
    kept = [line for index, line in enumerate(lines) if index not in replaced and line.strip() != MARK]
    if kept and not kept[-1].endswith(("\n", "\r")):
        kept[-1] += newline
    kept.append(MARK + newline)
    kept.append(f'user_pref("{PREF}", {"true" if enabled else "false"});' + newline)
    return before, "".join(kept).encode("latin-1")


def plan_local_writes(profile_dir: "Path | str", enabled: bool) -> dict[str, Any]:
    """What :func:`set_local_writes` would do, without writing anything. Raises ValueError for a bad profile."""
    before, updated = _plan(profile_dir, bool(enabled))
    return {"state": before, "after": _state_for(bool(enabled)), "changed": updated is not None}


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _atomic_write(target: Path, payload: bytes) -> None:
    """Write through a temporary file in the same folder, then replace the target. The original stays on a failure."""
    fd, temporary = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def set_local_writes(profile_dir: "Path | str", enabled: bool) -> dict[str, Any]:
    """Set the open local writes pref in the profile's user.js. Returns ``state`` (before), ``after`` and ``changed``.

    Raises ValueError, and writes nothing, when the folder is not a profile (no prefs.js). The write is atomic.
    When the state already matches and one line sets it, nothing is written and ``changed`` is False.
    """
    before, updated = _plan(profile_dir, bool(enabled))
    if updated is not None:
        _atomic_write(user_js_path(profile_dir), updated)
    return {"state": before, "after": _state_for(bool(enabled)), "changed": updated is not None}


# ---------------------------------------------------------------------------
# Restarting Zotero so the setting applies
# ---------------------------------------------------------------------------


def _close_zotero() -> str:
    from meridian import zotero_plugin_install as installer  # imported here: see the module docstring

    return installer.close_zotero_gracefully()


def _no_pre_launch_work() -> None:
    """The launcher's pre-launch hook, replaced with nothing: a restart for this setting installs no plugin."""
    return None


def _start_zotero() -> Any:
    from meridian import zotero_launcher  # imported here: see the module docstring

    return zotero_launcher.start_zotero(before_launch=_no_pre_launch_work)


_NOT_STARTED_TEXT = {
    "close_failed": (
        "Zotero did not close, so it was not started again. Close Zotero yourself, then start it. "
        "The setting applies at the next start."
    ),
    "unsupported": (
        "Meridian cannot close Zotero on this computer, so Zotero was not restarted. "
        "Restart Zotero yourself for the setting to apply."
    ),
}
_NOT_RESTARTED_TEXT = "Zotero was not restarted. Restart Zotero yourself for the setting to apply."
_START_FAILED_TEXT = "Zotero could not be started. Start Zotero yourself."
# (close result, launch result) -> message. The close result is "closed" or "not_running" here.
_START_TEXT = {
    ("closed", "started"): "Zotero closed and was started again, so the setting applies.",
    ("closed", "already_running"): "Zotero closed and is running again, so the setting applies.",
    ("closed", "timeout"): (
        "Zotero closed and was started again, but its local API did not answer in time. Check that Zotero is running."
    ),
    ("closed", "not_installed"): (
        "Zotero closed, but no Zotero program was found, so it was not started. Start Zotero yourself."
    ),
    ("not_running", "started"): "Zotero was not running, so it was started. The setting applies.",
    ("not_running", "already_running"): "Zotero is running, so the setting applies.",
    ("not_running", "timeout"): (
        "Zotero was started, but its local API did not answer in time. Check that Zotero is running."
    ),
    ("not_running", "not_installed"): (
        "Zotero is not running, and no Zotero program was found, so it was not started. Start Zotero yourself."
    ),
}


def _started_text(closed: str, action: str) -> str:
    """The message after a close that let Zotero start. ``closed`` is the close result, ``action`` the launch result."""
    return _START_TEXT.get((closed, action), _START_FAILED_TEXT)


# The launch results that leave Zotero running. "not_installed" means no program was started.
_RUNNING_ACTIONS = ("started", "already_running", "timeout")


def restart_zotero(
    close: "Callable[[], str] | None" = None,
    start: "Callable[[], Any] | None" = None,
) -> "tuple[bool, str]":
    """Close Zotero gracefully, then start it again, so a changed setting takes effect. Never raises.

    Returns ``(started, message)``. ``started`` is True only when Zotero is running again after the call: the close
    stopped it (or it was not open) and the launch reported that Zotero is running. ``close`` defaults to
    ``zotero_plugin_install.close_zotero_gracefully``. That only ever sends a normal close request and never
    force-kills. ``start`` defaults to ``zotero_launcher.start_zotero``. Zotero is started only when the close reports
    ``closed`` (it stopped) or ``not_running`` (it was not open). Any other result leaves Zotero as it is.
    """
    try:
        closed = str((close if close is not None else _close_zotero)() or "")
    except Exception:  # noqa: BLE001 -- a close that raises is a close that did not happen
        closed = "close_failed"
    if closed not in ("closed", "not_running"):
        return False, _NOT_STARTED_TEXT.get(closed, _NOT_RESTARTED_TEXT)
    try:
        launched = (start if start is not None else _start_zotero)()
    except Exception as exc:  # noqa: BLE001 -- a launch that raises is reported, never raised
        return False, f"Zotero could not be started ({exc}). Start Zotero yourself."
    action = str(launched.get("action") if isinstance(launched, Mapping) else launched or "")
    return action in _RUNNING_ACTIONS, _started_text(closed, action)


# ---------------------------------------------------------------------------
# Locating the default profile (the same one the installer uses)
# ---------------------------------------------------------------------------


def default_profile_dir() -> "Path | None":
    """The default Zotero profile folder, found as the installer finds it. None when there is none. Never raises."""
    from meridian import zotero_plugin_install as installer  # imported here: see the module docstring

    try:
        return installer.find_default_profile_dir(installer.zotero_data_dir())
    except Exception:  # noqa: BLE001 -- an unknown profile is reported as none
        return None


# ---------------------------------------------------------------------------
# CLI: python -m meridian zotero local-writes status|on|off [--profile PATH] [--restart] [--dry-run]
# ---------------------------------------------------------------------------

_RESTART_HINT = (
    "Zotero must restart for this change to take effect. Restart Zotero, or run this command again with --restart."
)
_NO_PROFILE_TEXT = (
    "No Zotero profile was found. Open Zotero once so it creates a profile, or pass --profile with its folder."
)


def _print_status(profile: Path) -> int:
    try:
        state = read_state(profile)
    except (OSError, ValueError) as exc:
        print(f"Local writes could not be read from {user_js_path(profile)}: {exc}")
        return 1
    print(f"Local writes are {state_text(state)} in {user_js_path(profile)}.")
    return 0


def _print_change(change: Mapping[str, Any], profile: Path) -> None:
    path = user_js_path(profile)
    if change["changed"]:
        print(
            f"Local writes are now {change['after']} in {path}. Before, the setting was {state_text(change['state'])}."
        )
    else:
        print(f"Local writes are already {change['after']} in {path}. Nothing was changed.")


def _print_dry_run(profile: Path, enabled: bool, *, restart: bool) -> int:
    print("Dry run: nothing will be written.")
    print(f"Zotero profile: {profile}")
    try:
        plan = plan_local_writes(profile, enabled)
    except (OSError, ValueError) as exc:
        print(f"Local writes could not be read: {exc}")
        return 1
    if plan["changed"]:
        print(
            f"Would set local writes to {plan['after']} in {user_js_path(profile)}. "
            f"The setting is {state_text(plan['state'])} now."
        )
    else:
        print(f"Local writes are already {plan['after']}. Nothing would change.")
    if restart:
        print("Would close Zotero gracefully and start it again, because of --restart.")
    elif plan["changed"]:
        print("Zotero would then need a restart for this to take effect. Use --restart to do that.")
    return 0


def cli_main(argv: "list[str] | None" = None) -> int:
    """``python -m meridian zotero local-writes``. Returns 0 on success, 1 otherwise.

    ``status`` prints the setting. ``on`` and ``off`` set it. ``--restart`` is the only way this command closes Zotero:
    it closes Zotero gracefully (never a force kill) and starts it again after the change, even when the file did not
    change. With ``--restart``, a run in which Zotero was not restarted returns 1. Without it, the command says that
    Zotero must restart. ``--dry-run`` prints the plan and writes nothing.
    """
    parser = argparse.ArgumentParser(
        prog="python -m meridian zotero local-writes",
        description=(
            "Show or set the Zotero bridge's open local writes setting in a profile's user.js. Off (the default) means "
            "the bridge asks at the desktop for each write. On means writes need no prompt. Zotero reads the setting "
            "when it starts."
        ),
    )
    parser.add_argument("action", choices=("status", "on", "off"), help="status shows the setting; on or off sets it.")
    parser.add_argument(
        "--profile", type=Path, default=None,
        help="Zotero profile folder (default: the profile Zotero marks as default).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the plan and write nothing.")
    parser.add_argument(
        "--restart",
        action="store_true",
        help="After on or off, close Zotero gracefully and start it again, even when the file did not change. "
        "Meridian never force-closes Zotero.",
    )
    args = parser.parse_args(argv)
    profile = Path(args.profile) if args.profile is not None else default_profile_dir()
    if profile is None:
        print(_NO_PROFILE_TEXT)
        return 1
    try:
        _require_profile(profile)
    except ValueError as exc:
        print(str(exc))
        return 1
    if args.action == "status":
        return _print_status(profile)
    enabled = args.action == "on"
    if args.dry_run:
        return _print_dry_run(profile, enabled, restart=args.restart)
    try:
        change = set_local_writes(profile, enabled)
    except (OSError, ValueError) as exc:
        print(f"Local writes were not changed: {exc}")
        return 1
    _print_change(change, profile)
    if args.restart:
        started, message = restart_zotero()
        print(message)
        if not started:
            return 1
    elif change["changed"]:
        print(_RESTART_HINT)
    return 0
