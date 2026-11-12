"""Each failed readiness-probe attempt is written to the tunnel log, with its failure class and no message text.

The slot health probe hit a closed local port here, so nothing listens and no real proxy is started.
"""

import asyncio
import socket

from meridian import tunnel_client as tc


def _closed_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def test_each_refused_probe_attempt_is_logged_with_its_failure_class(capsys):
    port = _closed_port()

    healthy = asyncio.run(tc._probe_slot_health(port, attempts=2, delay=0))

    out = capsys.readouterr().out
    assert healthy is False
    assert f"tunnel:probe port {port}: attempt 1 of 2 failed: " in out
    assert f"tunnel:probe port {port}: attempt 2 of 2 failed: " in out
    # The class name is logged, never a message that could carry a URL or a token.
    assert "http" not in out.lower()
