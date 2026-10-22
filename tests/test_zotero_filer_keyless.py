"""Keyless default for the Zotero filer CLI (--backend auto).

Auto uses the desktop connector whenever Zotero answers, and starts Zotero when
it does not. A real run never falls back to the Web API: when Zotero cannot be
brought up it reports zotero_unavailable and writes nothing. A dry run never
starts Zotero. It uses the desktop when Zotero already answers, and the Web API
only when Zotero is down and credentials are usable. Credentials are never read
on the desktop path.

Every test replaces the launcher probe and start, both backends and the
credential lookup, so nothing touches the real Zotero app, the network or any
real credential. An autouse fixture makes any unfaked launcher call fail loudly.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from meridian import zotero_filer as zf
from meridian import zotero_launcher as zl

SECRET = "SECRET-KEYLESS-TEST-VALUE-do-not-print"
CREDS = ("1234567", SECRET)
OK = {"status": "ok", "collection": "Research", "sent": 1, "added": 1, "skipped_duplicates": 0}
READY = {"action": "started", "exe": "C:/fake/zotero.exe", "api_ready": True}
NOT_READY = {"action": "timeout", "exe": "C:/fake/zotero.exe", "api_ready": False}
NOT_INSTALLED = {"action": "not_installed", "exe": None, "api_ready": False}
BIB = "@article{keyless2026,\n  title={Keyless Default},\n  year={2026}\n}\n"


class _Calls:
    """Records every call and returns a fixed value, or raises a fixed error."""

    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return self.value


@pytest.fixture(autouse=True)
def _no_real_zotero(monkeypatch):
    """Fail loudly if a test reaches the real launcher without declaring a fake."""

    def _refuse(*args, **kwargs):
        raise AssertionError("the real Zotero launcher must not run in tests")

    monkeypatch.setattr(zl, "zotero_api_alive", _refuse)
    monkeypatch.setattr(zl, "start_zotero", _refuse)


@pytest.fixture
def bib_path(tmp_path):
    path = tmp_path / "keyless.bib"
    path.write_text(BIB, encoding="utf-8")
    return str(path)


def _world(monkeypatch, *, alive, start=None, start_error=None, creds=None):
    """Install fakes for the probe, the start, the credentials and both backends."""
    world = SimpleNamespace(
        probes=_Calls(value=alive),
        starts=_Calls(value=start, error=start_error),
        reads=_Calls(value=creds),
        desktop=_Calls(value=OK),
        web=_Calls(value=OK),
    )
    monkeypatch.setattr(zl, "zotero_api_alive", world.probes)
    monkeypatch.setattr(zl, "start_zotero", world.starts)
    monkeypatch.setattr(zf, "web_credentials", world.reads)
    monkeypatch.setattr(zf, "file_bib_files", world.desktop)
    monkeypatch.setattr(zf, "file_bib_files_web", world.web)
    return world


def _file(bib, *extra):
    return zf.main(["file", "--collection", "Research", "--bib", bib, *extra])


def test_auto_uses_desktop_when_api_answers_and_never_reads_keys(bib_path, monkeypatch):
    world = _world(monkeypatch, alive=True, creds=CREDS)
    assert _file(bib_path) == 0
    assert len(world.desktop.calls) == 1
    assert world.web.calls == []
    assert world.starts.calls == []
    assert world.reads.calls == []


def test_auto_uses_desktop_when_api_answers_without_credentials(bib_path, monkeypatch):
    world = _world(monkeypatch, alive=True, creds=None)
    assert _file(bib_path) == 0
    assert len(world.desktop.calls) == 1
    assert world.web.calls == []
    assert world.starts.calls == []


def test_auto_starts_zotero_when_api_is_down_then_uses_desktop(bib_path, monkeypatch):
    world = _world(monkeypatch, alive=False, start=READY, creds=CREDS)
    assert _file(bib_path) == 0
    assert len(world.starts.calls) == 1
    assert len(world.desktop.calls) == 1
    assert world.web.calls == []


def test_auto_does_not_fall_back_to_web_when_start_does_not_bring_zotero_up(bib_path, monkeypatch, capsys):
    world = _world(monkeypatch, alive=False, start=NOT_READY, creds=CREDS)
    code = _file(bib_path)
    captured = capsys.readouterr()
    assert code == 2
    assert json.loads(captured.out)["status"] == "zotero_unavailable"
    assert len(world.starts.calls) == 1
    assert world.desktop.calls == []
    assert world.web.calls == []
    assert world.reads.calls == []
    assert "--backend web" in captured.err
    assert SECRET not in captured.out + captured.err


def test_auto_does_not_fall_back_to_web_when_zotero_cannot_be_launched(bib_path, monkeypatch, capsys):
    world = _world(monkeypatch, alive=False, start_error=OSError("launch refused"), creds=CREDS)
    code = _file(bib_path)
    captured = capsys.readouterr()
    assert code == 2
    assert json.loads(captured.out)["status"] == "zotero_unavailable"
    assert len(world.starts.calls) == 1
    assert world.desktop.calls == []
    assert world.web.calls == []
    assert world.reads.calls == []
    assert SECRET not in captured.out + captured.err


def test_auto_reports_zotero_unavailable_without_credentials(bib_path, monkeypatch, capsys):
    world = _world(monkeypatch, alive=False, start=NOT_INSTALLED, creds=None)
    code = _file(bib_path)
    captured = capsys.readouterr()
    assert code == 2
    assert json.loads(captured.out)["status"] == "zotero_unavailable"
    assert world.desktop.calls == []
    assert world.web.calls == []
    assert "Start Zotero" in captured.err
    assert SECRET not in captured.out + captured.err
    assert "ZOTERO_API_KEY" not in captured.out + captured.err
    assert "ZOTERO_USER_ID" not in captured.out + captured.err


def test_explicit_web_backend_still_uses_web_and_skips_the_desktop_probe(bib_path, monkeypatch):
    world = _world(monkeypatch, alive=True, creds=CREDS)
    assert _file(bib_path, "--backend", "web") == 0
    assert len(world.web.calls) == 1
    assert world.desktop.calls == []
    assert world.probes.calls == []
    assert world.starts.calls == []


def test_explicit_desktop_backend_skips_probe_start_and_keys(bib_path, monkeypatch):
    world = _world(monkeypatch, alive=False, creds=CREDS)
    assert _file(bib_path, "--backend", "desktop") == 0
    assert len(world.desktop.calls) == 1
    assert world.web.calls == []
    assert world.probes.calls == []
    assert world.starts.calls == []
    assert world.reads.calls == []


def test_auto_reports_unavailable_when_zotero_is_not_installed_and_never_uses_web(bib_path, monkeypatch, capsys):
    world = _world(monkeypatch, alive=False, start=NOT_INSTALLED, creds=CREDS)
    code = _file(bib_path)
    captured = capsys.readouterr()
    assert code == 2
    assert json.loads(captured.out)["status"] == "zotero_unavailable"
    assert world.desktop.calls == []
    assert world.web.calls == []
    assert world.reads.calls == []
    assert SECRET not in captured.out + captured.err


def test_dry_run_auto_with_desktop_down_and_no_credentials_exits_two_without_starting_zotero(
    bib_path, monkeypatch, capsys
):
    world = _world(monkeypatch, alive=False, start=READY, creds=None)
    code = _file(bib_path, "--dry-run")
    captured = capsys.readouterr()
    assert code == 2
    assert json.loads(captured.out)["status"] == "zotero_unavailable"
    assert world.starts.calls == []
    assert world.desktop.calls == []
    assert world.web.calls == []
    assert "Nothing was filed" in captured.err


def test_dry_run_auto_with_desktop_down_uses_web_and_never_starts_zotero(bib_path, monkeypatch, capsys):
    world = _world(monkeypatch, alive=False, start=READY, creds=CREDS)
    assert _file(bib_path, "--dry-run") == 0
    assert world.starts.calls == []
    assert world.desktop.calls == []
    assert len(world.web.calls) == 1
    assert world.web.calls[0][0][2:4] == CREDS
    assert world.web.calls[0][1]["dry_run"] is True
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err


def test_dry_run_auto_with_desktop_answering_uses_desktop_without_starting_zotero(bib_path, monkeypatch):
    world = _world(monkeypatch, alive=True, start=READY, creds=CREDS)
    assert _file(bib_path, "--dry-run") == 0
    assert world.starts.calls == []
    assert world.web.calls == []
    assert len(world.desktop.calls) == 1
    assert world.desktop.calls[0][1]["dry_run"] is True
