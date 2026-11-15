"""Host names from the owner's ssh config file, offered as suggestions for the GPU hosts. Pure Python: no Qt.

It reads one file, the ssh config in the owner's home folder (~/.ssh/config on Windows, macOS and Linux). From its Host
lines it takes only the names: a name with a wildcard (* or ?) or a negation (!) is skipped, and each name must pass
gpu_jobs.check_alias. Nothing else in the file is used: no key path, no ProxyCommand, no password, no Match block, and no
Include file is followed. A missing, unreadable or very large file gives no names, and nothing is raised.

The names are only suggestions. The Settings tab adds them to its text box when the owner presses the button, and they are
saved only when the owner presses Save. The GPUs tab then reads each saved name in batch mode, as before.
"""

from __future__ import annotations

import re
from pathlib import Path

from meridian_desktop.gpu_jobs import check_alias

CONFIG_RELATIVE = (".ssh", "config")
# A config file larger than this is not read. Real ones are a few kilobytes, so this only stops a runaway file.
MAX_CONFIG_BYTES = 256 * 1024
_HOST_LINE = re.compile(r"^\s*Host(?:\s*=\s*|\s+)(?P<names>.+?)\s*$", re.IGNORECASE)


def default_config_path(home: Path | None = None) -> Path:
    """The ssh config in the owner's home folder. ``home`` is for tests; None means the real home folder."""
    return (Path.home() if home is None else home).joinpath(*CONFIG_RELATIVE)


def host_names(text: str) -> tuple[str, ...]:
    """The usable host names in the text of an ssh config, in file order, with no repeats.

    Comment lines are skipped. A Host line may name several hosts, separated by spaces.
    """
    names: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _HOST_LINE.match(line)
        if match is None:
            continue
        for token in match.group("names").split():
            if "*" in token or "?" in token or token.startswith("!"):
                continue
            if check_alias(token):
                names.append(token)
    return tuple(dict.fromkeys(names))


def read_host_names(home: Path | None = None) -> tuple[str, ...]:
    """The host names from the owner's ssh config. Reads one file, and never raises: no file gives no names."""
    path = default_config_path(home)
    try:
        if not path.is_file() or path.stat().st_size > MAX_CONFIG_BYTES:
            return ()
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    return host_names(text)


__all__ = ["MAX_CONFIG_BYTES", "default_config_path", "host_names", "read_host_names"]
