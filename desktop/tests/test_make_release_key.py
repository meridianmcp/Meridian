"""Tests for packaging/make_release_key.py: the one-time seed, what it prints, and what it refuses.

Every seed lives under tmp_path. %LOCALAPPDATA% is pointed at a temp folder too, so no test can read or write the
real release seed.
"""

from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load():
    name = "make_release_key_under_test"
    spec = importlib.util.spec_from_file_location(name, REPO / "packaging" / "make_release_key.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mrk = _load()


@pytest.fixture(autouse=True)
def local_appdata(tmp_path, monkeypatch):
    folder = tmp_path / "localappdata"
    folder.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(folder))
    return folder


def test_creates_a_32_byte_seed_as_base64_when_none_exists(tmp_path, capsys):
    seed_path = tmp_path / "keys" / "release-ed25519.key"
    assert mrk.main(["--seed-path", str(seed_path)]) == 0
    stored = base64.b64decode(seed_path.read_text(encoding="ascii").strip(), validate=True)
    assert len(stored) == 32
    assert "created a new seed" in capsys.readouterr().err


def test_stdout_holds_only_the_public_key(tmp_path, capsys):
    seed_path = tmp_path / "release-ed25519.key"
    mrk.main(["--seed-path", str(seed_path)])
    captured = capsys.readouterr()
    seed_text = seed_path.read_text(encoding="ascii").strip()

    lines = captured.out.splitlines()
    assert len(lines) == 1
    public = base64.b64decode(lines[0], validate=True)
    assert len(public) == 32
    assert public == mrk.ed25519.public_key(base64.b64decode(seed_text))
    assert seed_text not in captured.out
    assert seed_text not in captured.err


def test_an_existing_seed_is_never_overwritten(tmp_path, capsys):
    seed_path = tmp_path / "release-ed25519.key"
    mrk.main(["--seed-path", str(seed_path)])
    before = seed_path.read_bytes()
    capsys.readouterr()

    assert mrk.main(["--seed-path", str(seed_path)]) == 0
    assert seed_path.read_bytes() == before
    captured = capsys.readouterr()
    assert "already exists and was not changed" in captured.err
    assert len(captured.out.splitlines()) == 1


def test_an_existing_seed_prints_the_same_public_key(tmp_path, capsys):
    seed_path = tmp_path / "release-ed25519.key"
    mrk.main(["--seed-path", str(seed_path)])
    first = capsys.readouterr().out
    mrk.main(["--seed-path", str(seed_path)])
    assert capsys.readouterr().out == first


def test_create_seed_reports_false_when_the_file_exists(tmp_path):
    seed_path = tmp_path / "release-ed25519.key"
    assert mrk.create_seed(seed_path) is True
    assert mrk.create_seed(seed_path) is False


@pytest.mark.parametrize(
    "text",
    [
        "not base64 at all!!",
        base64.b64encode(bytes(31)).decode("ascii"),
        base64.b64encode(bytes(33)).decode("ascii"),
    ],
)
def test_a_damaged_seed_is_refused_and_never_printed(tmp_path, capsys, text):
    seed_path = tmp_path / "release-ed25519.key"
    seed_path.write_text(text + "\n", encoding="ascii")
    assert mrk.main(["--seed-path", str(seed_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not base64 of 32 bytes" in captured.err
    assert text not in captured.err
    assert seed_path.read_text(encoding="ascii") == text + "\n"


def test_read_seed_reports_a_missing_file_without_the_absolute_path(tmp_path):
    with pytest.raises(mrk.SeedError) as caught:
        mrk.read_seed(tmp_path / "absent" / "release-ed25519.key")
    message = str(caught.value)
    assert "missing" in message
    assert "%LOCALAPPDATA%" in message
    assert str(tmp_path) not in message


def test_default_seed_path_is_under_localappdata(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert mrk.default_seed_path() == tmp_path / "meridian-release-keys" / "release-ed25519.key"


def test_the_default_seed_is_under_local_app_data_and_never_in_the_repository(local_appdata):
    path = mrk.default_seed_path()
    assert path == local_appdata / "meridian-release-keys" / "release-ed25519.key"
    assert REPO not in path.parents


def test_without_local_app_data_the_default_is_the_home_app_data_folder_outside_the_repository(
    tmp_path, monkeypatch
):
    # The publisher resolves the same default, so both tools agree on where the seed lives.
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(mrk.Path, "home", lambda: tmp_path / "home")
    path = mrk.default_seed_path()
    assert path == tmp_path / "home" / "AppData" / "Local" / "meridian-release-keys" / "release-ed25519.key"
    assert REPO not in path.parents


def test_main_without_a_seed_path_creates_the_seed_under_local_app_data(local_appdata, capsys):
    assert mrk.main([]) == 0
    seed_path = local_appdata / "meridian-release-keys" / "release-ed25519.key"
    stored = base64.b64decode(seed_path.read_text(encoding="ascii").strip(), validate=True)
    assert len(stored) == 32
    printed = capsys.readouterr().out.splitlines()
    assert printed == [base64.b64encode(mrk.ed25519.public_key(stored)).decode("ascii")]
