"""Tests for meridian_desktop/slot_ports.py: the slot port table, the connect-only probe and its labels.

The probe is run against real loopback sockets. A stub listener on an ephemeral port records
every byte a client sends, so "sends zero bytes" is checked on the wire and not only in the
code. No test uses the real slot ports (8808 to 8822), which may be in use on this computer.
"""

import ast
import inspect
import socket
import threading
from pathlib import Path

import pytest

from meridian_desktop import slot_ports
from meridian_desktop.slot_ports import (
    LISTENING_LABEL,
    NOT_LISTENING_LABEL,
    PER_PORT_TIMEOUT,
    PROBE_HOST,
    SLOT_PORTS,
    SlotPortResult,
    probe_slot_ports,
    worst_case_seconds,
)

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "meridian_desktop" / "slot_ports.py"

# Typed in by hand from the core repository's tunnel_plugins.py: SLOTS, the DEFAULT_*_PORT
# values and CUSTOM_SLOT_PORTS (p0 to p3). The test pins these literals on purpose.
EXPECTED_SLOT_PORTS = (
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

# Attribute names a socket would need for sending or reading. The probe module uses none.
SOCKET_IO_NAMES = {
    "send",
    "sendall",
    "sendto",
    "sendmsg",
    "recv",
    "recv_into",
    "recvfrom",
    "recvfrom_into",
    "recvmsg",
    "makefile",
    "read",
    "write",
}
ALLOWED_IMPORT_ROOTS = {"__future__", "collections", "dataclasses", "socket", "typing"}


def _closed_port() -> int:
    """A loopback port that nothing listens on right now."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class _RecordingListener:
    """A loopback listener that takes one connection, reads until the client closes, and keeps the bytes.

    ``received`` holds every byte the client sent. ``connections`` counts accepted connections.
    ``wait_for_connection`` returns once the one connection has ended, or the accept timed out.
    """

    def __init__(self) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(4)
        self._server.settimeout(5.0)
        self.port = self._server.getsockname()[1]
        self.received = b""
        self.connections = 0
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> "_RecordingListener":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._thread.join(timeout=10)
        self._server.close()

    def _serve(self) -> None:
        chunks: list[bytes] = []
        try:
            conn, _ = self._server.accept()
        except OSError:
            self._done.set()
            return
        self.connections += 1
        try:
            conn.settimeout(5.0)
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except OSError:
            pass
        finally:
            conn.close()
            self.received = b"".join(chunks)
            self._done.set()

    def wait_for_connection(self, timeout: float) -> bool:
        return self._done.wait(timeout)


def test_slot_port_probe_sends_no_bytes_and_covers_all_ports():
    # The table holds the core's literal ports: eleven built-ins (word included, LaTeX last) and p0 to p3.
    assert SLOT_PORTS == EXPECTED_SLOT_PORTS
    assert len(SLOT_PORTS) == 15

    # One check per port, in table order, each on 127.0.0.1 with the 0.25 second timeout.
    seen: list[tuple[str, int, float]] = []

    def record(host, port, timeout):
        seen.append((host, port, timeout))
        return False

    results = probe_slot_ports(check=record)
    assert [(r.slot, r.port) for r in results] == list(EXPECTED_SLOT_PORTS)
    assert seen == [(PROBE_HOST, port, PER_PORT_TIMEOUT) for _slot, port in EXPECTED_SLOT_PORTS]
    assert PROBE_HOST == "127.0.0.1"
    assert PER_PORT_TIMEOUT == 0.25

    # The module calls no send, read or file method on any socket.
    tree = ast.parse(MODULE_PATH.read_bytes(), filename=str(MODULE_PATH))
    attribute_names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not attribute_names & SOCKET_IO_NAMES

    # On the wire: a live loopback listener sees a connect, then zero bytes, then the close.
    closed = _closed_port()
    with _RecordingListener() as listener:
        results = probe_slot_ports(ports=(("stub", listener.port), ("closed", closed)))
        assert [(r.slot, r.listening) for r in results] == [("stub", True), ("closed", False)]
        assert listener.wait_for_connection(timeout=5.0)
        assert listener.connections == 1
        assert listener.received == b""


def test_probe_results_are_labelled_listening_never_healthy():
    results = probe_slot_ports(check=lambda host, port, timeout: port % 2 == 0)

    assert {r.label for r in results} == {LISTENING_LABEL, NOT_LISTENING_LABEL}
    assert LISTENING_LABEL == "listening"
    assert NOT_LISTENING_LABEL == "not listening"
    assert "healthy" not in SlotPortResult.__dataclass_fields__
    assert "listening" in SlotPortResult.__dataclass_fields__
    for result in results:
        assert isinstance(result.listening, bool)
        assert "healthy" not in result.label


def test_slot_port_timeout_is_per_port_and_total_is_bounded():
    assert PER_PORT_TIMEOUT == 0.25
    assert worst_case_seconds() == pytest.approx(3.75)
    assert worst_case_seconds() <= 3.75
    assert worst_case_seconds(EXPECTED_SLOT_PORTS[:2]) == pytest.approx(0.5)


def test_probe_is_loopback_only_and_takes_no_host_argument():
    assert "host" not in inspect.signature(probe_slot_ports).parameters
    results = probe_slot_ports(ports=(("stub", _closed_port()),))
    assert len(results) == 1 and results[0].listening is False


def test_probe_module_imports_no_qt_and_no_network_module_but_socket():
    tree = ast.parse(MODULE_PATH.read_bytes(), filename=str(MODULE_PATH))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= ALLOWED_IMPORT_ROOTS
    assert "PySide6" not in imported
    assert slot_ports.PROBE_HOST == "127.0.0.1"
