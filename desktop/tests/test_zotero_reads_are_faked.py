"""The suite reads no Zotero state from the machine. A default read would launch tasklist or open the API port.

conftest.py replaces the two default readers: the process list and the loopback GET. These tests call the default
status() with no reader passed, and fail if a process is launched or a socket is opened, or if the API is read as
anything but not running. The result therefore does not depend on whether the owner's Zotero is open.
"""

import subprocess
import urllib.request

from meridian_desktop import zotero_client


def test_a_default_zotero_status_launches_no_process_and_opens_no_socket(monkeypatch):
    launched = []
    opened = []

    def record_launch(*args, **kwargs):
        launched.append(args)
        raise AssertionError("a test launched a process to read Zotero")

    def record_opener(*args, **kwargs):
        opened.append(args)
        raise OSError("no socket in tests")

    monkeypatch.setattr(subprocess, "run", record_launch)
    monkeypatch.setattr(urllib.request, "build_opener", record_opener)

    status = zotero_client.status(env={})

    assert launched == []
    assert opened == []
    assert status.process_running is False
    assert status.api.state == zotero_client.API_NOT_RUNNING
