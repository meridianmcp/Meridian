"""Settings store for the desktop shell. Pure Python: no Qt import.

File: %LOCALAPPDATA%\\meridian-desktop\\settings.json, or
~/.meridian-desktop/settings.json when LOCALAPPDATA is unset.

Reads never raise. A missing file gives defaults with no warning. A corrupt or
unreadable file gives defaults plus a warning string, with the tunnel switch off
(fail closed, see load). Writes are atomic: a temp file in the same directory,
then os.replace.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Mapping

from meridian_desktop.gpu_jobs import MAX_HOSTS, check_alias, parse_host_list

# local: the Meridian server or database file on this computer. hosted: the hosted Meridian account.
# The tunnel belongs to hosted: hosted tools reach this computer through it, and hosted mode shows its status. A saved
# file with local or hosted reads exactly as it did before. An older saved "tunnel" reads as hosted (LEGACY_MODE_ALIASES).
VALID_MODES = ("local", "hosted")
# Older saved values that now mean another mode.
LEGACY_MODE_ALIASES = {"tunnel": "hosted"}
DEFAULT_MODE = "local"
DEFAULT_HOSTED_URL = "https://usemeridian.us"
DEFAULT_LOCAL_URL = "http://127.0.0.1:7878"
DEFAULT_ENGINE_URL = "http://127.0.0.1:8471"

_URL_KEYS = ("hosted_url", "local_url", "engine_url")

# The Meridian core checkout (the folder that holds the meridian package) and a Python program that
# can import it. The client setup runner uses both. Only paths are stored, never a secret.
CORE_CHECKOUT_KEY = "core_checkout"
CORE_PYTHON_KEY = "core_python"
CORE_KEYS = (CORE_CHECKOUT_KEY, CORE_PYTHON_KEY)
# True when the locator (meridian_desktop/core_locator.py) found that core path, and the owner did not choose it. The
# locator may replace an automatically found path, and never a path the owner saved by hand. Absent means false.
CORE_CHECKOUT_AUTO_KEY = "core_checkout_auto"
CORE_PYTHON_AUTO_KEY = "core_python_auto"
CORE_AUTO_FLAGS = {CORE_CHECKOUT_KEY: CORE_CHECKOUT_AUTO_KEY, CORE_PYTHON_KEY: CORE_PYTHON_AUTO_KEY}
CORE_AUTO_KEYS = tuple(CORE_AUTO_FLAGS.values())

# The mode a run starts in when the file names none and a hosted token is saved (owner decision, 2026-10-23).
HOSTED_MODE = "hosted"

# The three owner-confirmed control switches. Each is a real true or false. An absent key means the default in
# CONTROL_FLAG_DEFAULTS, which is set once below the Settings class.
CONTROL_FLAG_KEYS = ("tunnel_control_enabled", "zotero_autostart", "local_server_autostart", "tunnel_autostart")

# The exact values the owner saw in the confirmation dialog when a launch-time switch was turned on (amendments A and B
# of docs/local-trust-contract.md). The launch-time starts run only with these, and never with the current core setup.
# An empty value means the owner has not confirmed one yet.
CONFIRMED_KEYS = ("confirmed_core_python", "confirmed_core_checkout", "confirmed_zotero_exe")


# The owner limits on the Settings tab (the Limits group). Each is a whole number with a default and a range. A saved value
# outside its range is clamped to the nearer end, and a value that is not a whole number reads as the default. The
# server enforces them, and that enforcement is not active yet, so these values are stored and shown but stop nothing yet.
@dataclass(frozen=True)
class LimitSpec:
    key: str
    default: int
    minimum: int
    maximum: int


LIMIT_SPECS = (
    LimitSpec("limit_approval_total_mb", default=20480, minimum=1024, maximum=1048576),
    LimitSpec("limit_chat_total_mb", default=500, minimum=10, maximum=5000),
    LimitSpec("limit_file_mb", default=50, minimum=1, maximum=500),
    LimitSpec("limit_zip_download_mb", default=1000, minimum=10, maximum=10000),
    LimitSpec("limit_writes", default=500, minimum=10, maximum=10000),
    LimitSpec("limit_claims", default=20, minimum=1, maximum=200),
)
LIMIT_KEYS = tuple(spec.key for spec in LIMIT_SPECS)
_LIMIT_BY_KEY = {spec.key: spec for spec in LIMIT_SPECS}


def _limit_default(key: str) -> int:
    """The default of one owner limit, read from LIMIT_SPECS so a field and its range can never drift apart."""
    return _LIMIT_BY_KEY[key].default


def clamp_limit(key: str, value: int) -> int:
    """The value moved into the range of one owner limit: below the minimum gives the minimum, above the maximum the maximum.

    Raises ValueError when the key is not an owner limit.
    """
    spec = _LIMIT_BY_KEY.get(key)
    if spec is None:
        raise ValueError(f"unknown limit: {key}")
    return min(max(value, spec.minimum), spec.maximum)


@dataclass(frozen=True)
class Settings:
    mode: str = DEFAULT_MODE
    # True when the file names a valid mode, which the owner chose. A missing file, a corrupt file, or a file with no
    # mode key has no explicit mode: the run then starts in the launch mode (see resolve_launch_mode), and save() does
    # not write a mode key, so the file keeps none until the owner picks one.
    mode_explicit: bool = True
    hosted_url: str = DEFAULT_HOSTED_URL
    local_url: str = DEFAULT_LOCAL_URL
    engine_url: str = DEFAULT_ENGINE_URL
    # Path to the Meridian database file that the read-only reader opens. None means the
    # user has not picked one yet. Only a path is stored here, never a token or a secret.
    db_path: str | None = None
    # Start Meridian Desktop when the user signs in to Windows. Off by default. install.py
    # writes and removes the per-user Run entry. save() writes this key only when it is on,
    # so a file that never opted in keeps the five keys it has always had.
    autostart: bool = False
    # The core checkout and its Python, both "" when not set. save() writes each one only when it
    # is set, so a file that never set them keeps the keys it had before.
    core_checkout: str = ""
    core_python: str = ""
    # Check GitHub once at a real start for a newer release. The check makes one request for the release
    # JSON and downloads nothing. On by default. save() writes this key only when it is off, so a file that
    # never turned the check off is written exactly as before.
    check_updates: bool = True
    # Install new releases in the background, and check for them once at launch and every six hours after. On by
    # default. The Settings tab shows it as a checkbox. save() writes this key only when it is off. A check runs only
    # when both this and check_updates are on.
    auto_update: bool = True
    # The owner-confirmed runner controls (docs/local-trust-contract.md). save() writes a key only when it differs from
    # its default, so a file that keeps the defaults keeps the keys it had before. tunnel_control_enabled switches the
    # tunnel start, stop and restart buttons, and defaults to true by owner decision on 2026-10-23 (amendment F). Each
    # button still asks first. A malformed value, and a file that is corrupt or unreadable, read it as false, so the
    # buttons fail closed. zotero_autostart and local_server_autostart start Zotero and the local server once at
    # launch, default to off, and are turned on only after the owner confirms the dialog.
    tunnel_control_enabled: bool = True
    zotero_autostart: bool = False
    local_server_autostart: bool = False
    # tunnel_autostart starts the hosted tunnel once at launch, only in Hosted mode. Default off, and turned
    # on only after the owner confirms the dialog that names the working folder claude.ai can reach (amendment G).
    tunnel_autostart: bool = False
    confirmed_core_python: str = ""
    confirmed_core_checkout: str = ""
    confirmed_zotero_exe: str = ""
    # Whether the core checkout and Python were found by the locator rather than chosen by the owner. False by default,
    # and save() writes each one only when it is True.
    core_checkout_auto: bool = False
    core_python_auto: bool = False
    # The names of the saved API keys that are not built in (RunPod is always listed). Only names: a value lives in the
    # keychain and never here. save() writes the key only when there is a name, so a file with no keys is unchanged.
    key_names: tuple[str, ...] = ()
    # The listed names of the API keys the owner marked critical, RunPod included. A critical key is listed first and
    # shows a badge. Only names: the mark never sits with a value. save() writes the key only when there is a name.
    critical_keys: tuple[str, ...] = ()
    # The MCP tools the owner switched off, as "group:tool" keys (meridian_desktop/tool_catalog.py). Every tool is on by
    # default, so an empty tuple is the default. save() writes the key only when it is not empty.
    disabled_tools: tuple[str, ...] = ()
    # The owner's own MCP connectors, one saved object each (meridian_desktop/custom_connectors.py). Each entry is checked
    # when it is read, so nothing in this list is trusted. save() writes the key only when there is an entry.
    custom_connectors: tuple[dict, ...] = ()
    # The owner's opt-in for claude.ai to reach custom connectors through the tunnel, while the tunnel is up. Off by default,
    # and save() writes it only when it is on.
    custom_tunnel_opt_in: bool = False
    # The SSH host names the GPUs tab reads over ssh (meridian_desktop/gpu_jobs.py). Only names from the owner's own ssh
    # config, never a key or a password. save() writes the key only when there is a name.
    gpu_hosts: tuple[str, ...] = ()
    # The Sessions tab's "Only projects with active sessions" switch (meridian_desktop/ui/sessions_panel.py). Off by
    # default. save() writes the key only when it is on, so a file that never turned it on keeps the keys it had.
    sessions_only_active_projects: bool = False
    # The file-size note switch on the Limits group. On is the default. Off means the total-size note never shows. A
    # download is never asked about or blocked in either state, and virus scanning always applies.
    limit_approval_enabled: bool = True
    # The owner limits on the Settings tab's Limits group (LIMIT_SPECS). Each default is its LIMIT_SPECS entry. save()
    # writes a limit only when it differs from its default. These are stored only: enforcement happens on the server
    # and is not active yet.
    limit_approval_total_mb: int = _limit_default("limit_approval_total_mb")
    limit_chat_total_mb: int = _limit_default("limit_chat_total_mb")
    limit_file_mb: int = _limit_default("limit_file_mb")
    limit_zip_download_mb: int = _limit_default("limit_zip_download_mb")
    limit_writes: int = _limit_default("limit_writes")
    limit_claims: int = _limit_default("limit_claims")
    # The keys file the owner chose for the Keys block (keys_file.py). Only its path is stored, never a value. "" means no
    # file is chosen. save() writes the key only when a path is set, so a file that never chose one keeps no such key.
    keys_file_path: str = ""


# The default of each switch, read from Settings so the two can never drift apart.
CONTROL_FLAG_DEFAULTS = {key: getattr(Settings(), key) for key in CONTROL_FLAG_KEYS}


class SettingsUnreadable(Exception):
    """The settings file could not be read, so a write was refused. The message is the owner's text."""


SETTINGS_UNREADABLE_TEXT = (
    "settings.json could not be read, so nothing was saved. A copy is kept as settings.json.unreadable.bak."
)
# Said only when the copy could not be made, so the owner is never told a copy exists when it does not.
SETTINGS_UNREADABLE_NO_COPY_TEXT = "settings.json could not be read and no copy could be kept, so nothing was saved."


@dataclass(frozen=True)
class LoadResult:
    settings: Settings
    warning: str | None = None


def default_settings_path(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    env = os.environ if env is None else env
    local_app_data = env.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "meridian-desktop" / "settings.json"
    base = home if home is not None else Path.home()
    return base / ".meridian-desktop" / "settings.json"


def core_path_problem(key: str, value: str) -> str | None:
    """Plain words for why a core setting cannot be saved, or None when it can.

    An empty value means "not set" and is always allowed, so a setting can be cleared. A checkout
    must be an existing folder that holds the meridian package. A Python must be an existing file.
    """
    if key not in CORE_KEYS:
        raise ValueError(f"unknown core setting: {key}")
    text = value.strip()
    if not text:
        return None
    path = Path(text)
    if key == CORE_CHECKOUT_KEY:
        if not path.is_dir():
            return (
                f"Not saved. The Meridian core checkout folder does not exist: {text}. "
                "Choose the folder that holds the meridian package."
            )
        if not (path / "meridian" / "__init__.py").is_file():
            return (
                f"Not saved. {text} is not a Meridian core checkout: it has no meridian package. "
                "Choose the folder that holds meridian/."
            )
        return None
    if not path.is_file():
        return (
            f"Not saved. The Python program does not exist: {text}. "
            "Choose the python.exe that can import meridian."
        )
    return None


def _from_mapping(data: Mapping) -> tuple[Settings, list[str]]:
    problems: list[str] = []
    values: dict[str, object] = {"mode_explicit": False}

    # mode: a valid mode in the file is explicit and always wins. An absent key is not an explicit mode, with no
    # warning, and neither is an invalid one, which is reported. The launch mode decides for those (see load).
    if "mode" in data:
        mode = data["mode"]
        if isinstance(mode, str):
            mode = LEGACY_MODE_ALIASES.get(mode, mode)
        if mode in VALID_MODES:
            values["mode"] = mode
            values["mode_explicit"] = True
        else:
            problems.append(f"mode must be one of {VALID_MODES}; the saved value is ignored")

    for key in _URL_KEYS:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, str) and value.strip():
            values[key] = value
        else:
            problems.append(f"{key} must be a non-empty string; using its default")

    # db_path: an absent key or null means "not chosen" (None) with no warning, so a file
    # saved with db_path unset reads back clean. A blank or non-string value is refused.
    if "db_path" in data:
        value = data["db_path"]
        if value is None:
            values["db_path"] = None
        elif isinstance(value, str) and value.strip():
            values["db_path"] = value
        else:
            problems.append("db_path must be a non-empty string or null; using None")

    # autostart: an absent key means off, with no warning. Only a real true or false is accepted.
    if "autostart" in data:
        value = data["autostart"]
        if isinstance(value, bool):
            values["autostart"] = value
        else:
            problems.append("autostart must be true or false; using false")

    # core_checkout and core_python: an absent key means "not set", with no warning. Only text is
    # accepted. Existence is not checked here, so a drive that is offline does not break loading.
    for key in CORE_KEYS:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, str):
            values[key] = value.strip()
        else:
            problems.append(f"{key} must be a path as text or empty; using empty")

    # The confirmed values: an absent key means not confirmed, with no warning. The text is kept exactly as saved,
    # because it is what the owner was shown. Only text is accepted.
    for key in CONFIRMED_KEYS:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, str):
            values[key] = value
        else:
            problems.append(f"{key} must be a path as text or empty; using empty")

    # core_checkout_auto and core_python_auto: an absent key means the owner chose the path (false), with no warning.
    for key in CORE_AUTO_KEYS:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, bool):
            values[key] = value
        else:
            problems.append(f"{key} must be true or false; using false")
    # key_names: an absent key means no saved names, with no warning. Only a list of non-empty text is accepted.
    if "key_names" in data:
        value = data["key_names"]
        if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
            values["key_names"] = tuple(item.strip() for item in value)
        else:
            problems.append("key_names must be a list of names; using none")

    # critical_keys: an absent key means no key is marked, with no warning. Only a list of non-empty text is accepted.
    if "critical_keys" in data:
        value = data["critical_keys"]
        if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
            values["critical_keys"] = tuple(dict.fromkeys(item.strip() for item in value))
        else:
            problems.append("critical_keys must be a list of names; using none")

    # keys_file_path: an absent key means no file is chosen, with no warning. Only text is accepted, and it is kept without
    # being checked here, so a file that is gone does not break loading. The Keys block says so when it reads the file.
    if "keys_file_path" in data:
        value = data["keys_file_path"]
        if isinstance(value, str):
            values["keys_file_path"] = value.strip()
        else:
            problems.append("keys_file_path must be a path as text or empty; using empty")

    # disabled_tools: an absent key means every tool is on, with no warning. Only a list of non-empty text is accepted.
    # A value that is not such a list is ignored with a warning, so no tool is switched off by a bad file.
    if "disabled_tools" in data:
        value = data["disabled_tools"]
        if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
            values["disabled_tools"] = tuple(dict.fromkeys(item.strip() for item in value))
        else:
            problems.append("disabled_tools must be a list of tool keys; using none")

    # custom_connectors: an absent key means none, with no warning. Only a list of objects is accepted here. Each object is
    # checked by custom_connectors.py, so one bad entry is shown on its own row and does not stop the rest.
    if "custom_connectors" in data:
        value = data["custom_connectors"]
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            values["custom_connectors"] = tuple(value)
        else:
            problems.append("custom_connectors must be a list of connector objects; using none")

    # custom_tunnel_opt_in: an absent key means off, with no warning. Only a real true or false is accepted.
    if "custom_tunnel_opt_in" in data:
        value = data["custom_tunnel_opt_in"]
        if isinstance(value, bool):
            values["custom_tunnel_opt_in"] = value
        else:
            problems.append("custom_tunnel_opt_in must be true or false; using false")

    # gpu_hosts: an absent key means none, with no warning. Only a list of valid SSH host names is accepted, and a bad
    # list is ignored with a warning, so no host is read from a file that does not check out.
    if "gpu_hosts" in data:
        value = data["gpu_hosts"]
        if isinstance(value, list) and len(value) <= MAX_HOSTS and all(check_alias(item) for item in value):
            values["gpu_hosts"] = tuple(dict.fromkeys(value))
        else:
            problems.append("gpu_hosts must be a list of SSH host names; using none")

    # sessions_only_active_projects: an absent key means off, with no warning. Only a real true or false is accepted.
    if "sessions_only_active_projects" in data:
        value = data["sessions_only_active_projects"]
        if isinstance(value, bool):
            values["sessions_only_active_projects"] = value
        else:
            problems.append("sessions_only_active_projects must be true or false; using false")

    # limit_approval_enabled: an absent key means on, with no warning. Only a real true or false is accepted.
    if "limit_approval_enabled" in data:
        value = data["limit_approval_enabled"]
        if isinstance(value, bool):
            values["limit_approval_enabled"] = value
        else:
            problems.append("limit_approval_enabled must be true or false; using true")

    # check_updates: an absent key means on, with no warning. Only a real true or false is accepted.
    if "check_updates" in data:
        value = data["check_updates"]
        if isinstance(value, bool):
            values["check_updates"] = value
        else:
            problems.append("check_updates must be true or false; using true")

    # auto_update: an absent key means on, with no warning. Only a real true or false is accepted.
    if "auto_update" in data:
        value = data["auto_update"]
        if isinstance(value, bool):
            values["auto_update"] = value
        else:
            problems.append("auto_update must be true or false; using true")

    # The runner control switches: an absent key means its default in CONTROL_FLAG_DEFAULTS (tunnel on, the two launch
    # switches off), with no warning. Only a real true or false is accepted. Any other value is read as false, never as
    # the default, so a malformed tunnel switch fails closed instead of turning the tunnel buttons on.
    for key in CONTROL_FLAG_KEYS:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, bool):
            values[key] = value
        else:
            values[key] = False
            problems.append(f"{key} must be true or false; using false")

    # The owner limits: an absent key means the default, with no warning. Only a whole number is accepted, and any other
    # value is ignored with a warning, so the default holds. A whole number outside its range is clamped to the nearer
    # end, and the owner is told the value that is used.
    for spec in LIMIT_SPECS:
        if spec.key not in data:
            continue
        value = data[spec.key]
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append(f"{spec.key} must be a whole number; using its default")
            continue
        clamped = clamp_limit(spec.key, value)
        if clamped != value:
            problems.append(f"{spec.key} must be between {spec.minimum} and {spec.maximum}; using {clamped}")
        values[spec.key] = clamped

    return replace(Settings(), **values), problems


REPLACE_ATTEMPTS = 20
REPLACE_WAIT_SECONDS = 0.05


def _replace_with_retry(source: str, target: Path) -> None:
    """Replace ``target`` with ``source``, waiting out a brief Windows file lock.

    On Windows a file another thread has open can refuse the replace for a moment. The write is retried for about a
    second. Any other error, or a lock that outlasts the retries, is raised to the caller unchanged.
    """
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_WAIT_SECONDS)


class SettingsStore:
    """Load and save the desktop settings at one path. The path is injectable.

    The launch mode is held for this run only. resolve_launch_mode sets it once at launch, and load() then gives it to
    any settings that name no explicit mode. save() never writes it, so the file stays as the owner left it.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path is not None else default_settings_path()
        self._launch_mode: str | None = None

    @property
    def path(self) -> Path:
        return self._path

    def resolve_launch_mode(self, hosted_token_saved: Callable[[], bool]) -> str:
        """Pick the mode this run starts in, hold it for the run, and return it.

        Call once, at launch, before the window.

        A mode the file names always wins. With no explicit mode, the run starts in hosted mode when a hosted token is
        saved, and in local mode otherwise. ``hosted_token_saved`` answers only whether a token exists. It is called
        only when the file has no explicit mode, and the token is never read here.
        """
        settings = self.load().settings
        if settings.mode_explicit:
            self._launch_mode = None
            return settings.mode
        self._launch_mode = HOSTED_MODE if hosted_token_saved() else DEFAULT_MODE
        return self._launch_mode

    def _for_this_run(self, settings: Settings) -> Settings:
        """The settings as this run sees them. A file with no explicit mode takes the launch mode, once one is resolved."""
        if settings.mode_explicit or self._launch_mode is None:
            return settings
        return replace(settings, mode=self._launch_mode)

    def load(self) -> LoadResult:
        # Fail closed for the tunnel switch (decision, X1). A missing file has no saved choice, so it keeps the default,
        # which is on (amendment F). A file that is there but cannot be used has a saved choice nobody can read, so the
        # tunnel switch is off until the owner fixes the file.
        no_file = replace(Settings(), mode_explicit=False)
        unusable = replace(no_file, tunnel_control_enabled=False)
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return LoadResult(self._for_this_run(no_file))
        except (OSError, ValueError) as exc:
            return LoadResult(
                self._for_this_run(unusable),
                f"settings file unreadable ({exc.__class__.__name__}); using defaults, with the tunnel controls off",
            )

        try:
            data = json.loads(raw)
        except ValueError:
            return LoadResult(
                self._for_this_run(unusable),
                "settings file is corrupt (not valid JSON); using defaults, with the tunnel controls off",
            )

        if not isinstance(data, dict):
            return LoadResult(
                self._for_this_run(unusable),
                "settings file does not hold a JSON object; using defaults, with the tunnel controls off",
            )

        settings, problems = _from_mapping(data)
        return LoadResult(self._for_this_run(settings), "; ".join(problems) or None)

    def save(self, settings: Settings) -> None:
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)
        data = asdict(settings)
        del data["mode_explicit"]  # a flag about the file, not a setting
        if not settings.mode_explicit:
            # No mode the owner chose: the file keeps no mode key, so a later launch can still pick one.
            del data["mode"]
        if not settings.autostart:
            # Off is the default, so a file that never opted in is written exactly as before.
            del data["autostart"]
        for key in CORE_KEYS + CONFIRMED_KEYS:
            if not data[key]:
                # "Not set" is the default, so a file that never set a core path or a confirmed value has no such key.
                del data[key]
        for key in CORE_AUTO_KEYS:
            if not data[key]:
                # False is the default, so only a path the locator found carries its flag.
                del data[key]
        if not settings.key_names:
            # No saved names is the default, so a file with no API keys has no such key.
            del data["key_names"]
        if not settings.critical_keys:
            # No critical marks is the default, so a file with none has no such key.
            del data["critical_keys"]
        if not settings.disabled_tools:
            # Every tool on is the default, so a file with no switched-off tool has no such key.
            del data["disabled_tools"]
        if not settings.custom_connectors:
            # No connectors is the default, so a file with none has no such key.
            del data["custom_connectors"]
        if not settings.custom_tunnel_opt_in:
            # Off is the default, so the tunnel opt-in is written only once the owner turns it on.
            del data["custom_tunnel_opt_in"]
        if not settings.gpu_hosts:
            # No saved hosts is the default, so a file with none has no such key.
            del data["gpu_hosts"]
        if not settings.sessions_only_active_projects:
            # Off is the default, so the Sessions tab's switch is written only once the owner turns it on.
            del data["sessions_only_active_projects"]
        if settings.limit_approval_enabled:
            # On is the default, so the file-size note switch is written only once the owner turns it off.
            del data["limit_approval_enabled"]
        if not settings.keys_file_path:
            # No file chosen is the default, so a file that never chose a keys file has no such key.
            del data["keys_file_path"]
        if settings.check_updates:
            # On is the default, so a file that never turned the update check off has no such key.
            del data["check_updates"]
        if settings.auto_update:
            # On is the default, so a file that never turned automatic updates off has no such key.
            del data["auto_update"]
        for key in CONTROL_FLAG_KEYS:
            if data[key] == CONTROL_FLAG_DEFAULTS[key]:
                # The default is never written, so a file that keeps the defaults has no such key.
                del data[key]
        for spec in LIMIT_SPECS:
            # A limit is always written inside its range, so the file never holds a value that load would have to correct.
            data[spec.key] = clamp_limit(spec.key, data[spec.key])
            if data[spec.key] == spec.default:
                # The default limit is never written, so a file that keeps the default limits has no such key.
                del data[spec.key]
        payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            _replace_with_retry(tmp_name, self._path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

    def keep_unreadable_copy(self) -> None:
        """Copy the settings file aside before a write that would replace it, when the file did not load cleanly.

        The copy is <name>.unreadable.bak beside the file. Nothing is copied for a file that loads cleanly or does not
        exist. Never raises: a failed copy must not stop the write the owner asked for.
        """
        if self.load().warning is None:
            return
        try:
            if self._path.is_file():
                shutil.copyfile(self._path, self._path.with_name(self._path.name + ".unreadable.bak"))
        except OSError:
            return

    def is_unreadable(self) -> bool:
        """True when the settings file is there but is not a JSON object, so no field in it can be trusted.

        A file with one bad value still reads as an object, and is not unreadable. A missing file is not unreadable.
        """
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False
        except (OSError, UnicodeDecodeError):
            return True
        try:
            data = json.loads(text)
        except ValueError:
            return True
        return not isinstance(data, dict)

    def _keep_copy(self) -> bool:
        """Copy the unreadable file to <name>.unreadable.bak. True only when the copy was made."""
        try:
            shutil.copyfile(self._path, self._path.with_name(self._path.name + ".unreadable.bak"))
        except OSError:
            return False
        return True

    def set_gpu_hosts(self, text: str) -> Settings:
        """Validate and save the GPUs tab's SSH host names, from one comma-separated line.

        Raises ValueError with plain words when a name is refused. A refused line writes nothing. An empty line clears
        the list. Every other field in the saved file is kept.

        A settings file that is not a JSON object is never rebuilt from defaults. It is copied aside when that works,
        nothing is written, and SettingsUnreadable is raised with the message the owner sees.
        """
        hosts, problems = parse_host_list(text)
        if problems:
            raise ValueError(" ".join(problems))
        if self.is_unreadable():
            copied = self._keep_copy()
            raise SettingsUnreadable(SETTINGS_UNREADABLE_TEXT if copied else SETTINGS_UNREADABLE_NO_COPY_TEXT)
        # A readable file with one bad value is copied aside before the write, as the other writers do.
        self.keep_unreadable_copy()
        updated = replace(self.load().settings, gpu_hosts=hosts)
        self.save(updated)
        return updated

    def set_sessions_only_active(self, value: bool) -> Settings:
        """Save the Sessions tab's "Only projects with active sessions" switch. Every other field in the file is kept.

        A settings file that is not a JSON object is never rebuilt from defaults. It is copied aside when that works,
        nothing is written, and SettingsUnreadable is raised with the message the owner sees.
        """
        if self.is_unreadable():
            copied = self._keep_copy()
            raise SettingsUnreadable(SETTINGS_UNREADABLE_TEXT if copied else SETTINGS_UNREADABLE_NO_COPY_TEXT)
        # A readable file with one bad value is copied aside before the write, as the other writers do.
        self.keep_unreadable_copy()
        updated = replace(self.load().settings, sessions_only_active_projects=bool(value))
        self.save(updated)
        return updated

    def set_mode(self, mode: str) -> Settings:
        mode = LEGACY_MODE_ALIASES.get(mode, mode)
        if mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {VALID_MODES}, got {mode!r}")
        # The owner chose this mode, so it is explicit from now on and wins over the launch mode.
        updated = replace(self.load().settings, mode=mode, mode_explicit=True)
        self.save(updated)
        return updated

    def set_core_path(self, key: str, value: str) -> Settings:
        """Validate and save one core setting. Raises ValueError with plain words when it is refused.

        A refused value writes nothing. Every other field in the saved file is kept, except that a saved core path turns
        off every launch-time switch (zotero_autostart, local_server_autostart and tunnel_autostart). The owner must
        confirm them again against the new setup. The caller says so in the Settings status line.
        """
        text = value.strip()
        problem = core_path_problem(key, text)
        if problem is not None:
            raise ValueError(problem)
        updated = replace(
            self.load().settings,
            **{key: text, CORE_AUTO_FLAGS[key]: False},
            zotero_autostart=False,
            local_server_autostart=False,
            tunnel_autostart=False,
        )
        self.save(updated)
        return updated

    def set_limit(self, key: str, value: int) -> Settings:
        """Clamp one owner limit to its range and save it. Every other field in the saved file is kept.

        Raises ValueError with plain words when the key is not an owner limit or the value is not a whole number. A refused
        call writes nothing. A value outside the range is saved as the nearer end of the range, and the saved settings are
        returned.

        A settings file that is not a JSON object is never rebuilt from defaults. It is copied aside when that works, nothing
        is written, and SettingsUnreadable is raised, as set_gpu_hosts does.
        """
        if key not in _LIMIT_BY_KEY:
            raise ValueError(f"unknown limit: {key}")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be a whole number.")
        if self.is_unreadable():
            copied = self._keep_copy()
            raise SettingsUnreadable(SETTINGS_UNREADABLE_TEXT if copied else SETTINGS_UNREADABLE_NO_COPY_TEXT)
        # A readable file with one bad value is copied aside before the write, as the other writers do.
        self.keep_unreadable_copy()
        updated = replace(self.load().settings, **{key: clamp_limit(key, value)})
        self.save(updated)
        return updated

    def set_limit_approval_enabled(self, value: bool) -> Settings:
        """Save the file-size note switch on the Limits group. Every other field in the file is kept.

        A download is never asked about or blocked by this switch, in either state. It only decides whether the total-size
        note shows. Raises ValueError for a value that is not true or false, and SettingsUnreadable like set_limit.
        """
        if not isinstance(value, bool):
            raise ValueError("limit_approval_enabled must be true or false.")
        if self.is_unreadable():
            copied = self._keep_copy()
            raise SettingsUnreadable(SETTINGS_UNREADABLE_TEXT if copied else SETTINGS_UNREADABLE_NO_COPY_TEXT)
        self.keep_unreadable_copy()
        updated = replace(self.load().settings, limit_approval_enabled=value)
        self.save(updated)
        return updated
