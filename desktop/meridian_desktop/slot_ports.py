"""Listening check for the tunnel slot ports on this computer. Pure Python: no Qt import.

A slot is a fixed loopback port that one tunnel plugin rides on. SLOT_PORTS copies the
table from the core repository's meridian/tunnel_plugins.py (the DEFAULT_*_PORT values and
CUSTOM_SLOT_PORTS). The test pins the literal values, so a change in the core is copied here
on purpose and never picked up by accident. The word slot is retired in the core, and its
port stays in the table so the probe still reports on it.

The probe asks one question per port: does something accept a TCP connection on 127.0.0.1?
It opens a socket, calls connect with a 0.25 second timeout, and closes it. It sends no
bytes, reads nothing and makes no MCP request. Any process on the port would accept the
connection, so a result says "listening" and never "healthy". Health needs a request to the
server, which this module never sends.

The ports are checked one after another, so the worst case is len(SLOT_PORTS) times
PER_PORT_TIMEOUT: 3.75 seconds for the 15 ports. See worst_case_seconds.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass

# The host is a constant, not a setting. The probe never leaves this computer.
PROBE_HOST = "127.0.0.1"
PER_PORT_TIMEOUT = 0.25

LISTENING_LABEL = "listening"
NOT_LISTENING_LABEL = "not listening"

# (slot, port): eleven built-in slots (the retired word slot included) and the four pre-allocated custom slots p0 to p3.
SLOT_PORTS: tuple[tuple[str, int], ...] = (
    ("fs", 8808),
    ("code", 8809),
    ("extract", 8810),
    ("ppt", 8811),
    ("word", 8812),
    ("dc", 8813),
    ("p0", 8814),
    ("p1", 8815),
    ("p2", 8816),
    ("p3", 8817),
    ("docs", 8818),
    ("zotero", 8819),
    ("outputs", 8820),
    ("debug", 8821),
    ("latex", 8822),
)

# check(host, port, timeout) -> True when something accepts a connection on host:port.
ConnectCheck = Callable[[str, int, float], bool]


@dataclass(frozen=True)
class SlotPortResult:
    """One port's result. ``listening`` is the only verdict; there is no health field."""

    slot: str
    port: int
    listening: bool

    @property
    def label(self) -> str:
        return LISTENING_LABEL if self.listening else NOT_LISTENING_LABEL


def _accepts_connection(host: str, port: int, timeout: float) -> bool:
    """True when a TCP connection to host:port is accepted. Sends and reads nothing.

    Any failure (refused, timed out, unreachable, or no socket at all) gives False.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return False
    try:
        sock.settimeout(timeout)
        sock.connect((host, port))
    except OSError:
        return False
    finally:
        sock.close()
    return True


def probe_slot_ports(
    ports: Sequence[tuple[str, int]] = SLOT_PORTS,
    *,
    check: ConnectCheck = _accepts_connection,
) -> list[SlotPortResult]:
    """Return one listening result per (slot, port), in the order given.

    ``check`` is called once per port as check(PROBE_HOST, port, PER_PORT_TIMEOUT). The
    default opens a connect-only socket. Tests pass a fake to see the calls without a socket.
    """
    return [
        SlotPortResult(slot, port, bool(check(PROBE_HOST, port, PER_PORT_TIMEOUT)))
        for slot, port in ports
    ]


def worst_case_seconds(ports: Sequence[tuple[str, int]] = SLOT_PORTS) -> float:
    """The longest the probe can take: each port uses its full timeout, one after another."""
    return len(ports) * PER_PORT_TIMEOUT


__all__ = [
    "LISTENING_LABEL",
    "NOT_LISTENING_LABEL",
    "PER_PORT_TIMEOUT",
    "PROBE_HOST",
    "SLOT_PORTS",
    "ConnectCheck",
    "SlotPortResult",
    "probe_slot_ports",
    "worst_case_seconds",
]
