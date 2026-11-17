"""Tests for MERIDIAN_INVENTREE_READ_ONLY: the read-only tool surface a tunnel exposes.

Read-only mode must leave inventree_propose out of the tool list, refuse any unknown value
without echoing it, and keep every read tool working. Every request goes through a fake
transport. Nothing here opens a socket.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from meridian_inventree import server
from meridian_inventree.server import ENV_READ_ONLY, ENV_STATE, ENV_TOKEN, ENV_URL, ConfigError, build_app, load_config

# tests/ is not a package; pytest's default import mode puts this folder on sys.path, so the
# sibling helper module imports by its plain name. Only helpers are imported, not its tests.
from test_server import BASE_URL, EXPECTED_TOOLS, READ_TOOLS, TOKEN, FakeTransport, _tool_list, _version_routes

READ_ONLY_TOOLS = READ_TOOLS  # the nine read tools plus inventree_plan_status (local store only)


def _env(state: Path, **extra: str) -> dict[str, str]:
    env = {ENV_URL: BASE_URL, ENV_TOKEN: TOKEN, ENV_STATE: str(state)}
    env.update(extra)
    return env


def _names(app: Any) -> set[str]:
    return {tool.name for tool in _tool_list(app)}


def test_default_mode_keeps_write_planning(tmp_path: Path) -> None:
    app = build_app(_env(tmp_path / "state"), transport=FakeTransport())
    assert _names(app) == EXPECTED_TOOLS
    assert "inventree_propose" in app.tools


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "on"])
def test_true_values_drop_propose_from_the_tool_list(tmp_path: Path, value: str) -> None:
    app = build_app(_env(tmp_path / "state", **{ENV_READ_ONLY: value}), transport=FakeTransport())
    assert _names(app) == READ_ONLY_TOOLS
    assert "inventree_propose" not in app.tools
    assert app.runtime.config.read_only is True


@pytest.mark.parametrize("value", ["", "0", "false", "NO", "off"])
def test_false_values_keep_write_planning(tmp_path: Path, value: str) -> None:
    app = build_app(_env(tmp_path / "state", **{ENV_READ_ONLY: value}), transport=FakeTransport())
    assert _names(app) == EXPECTED_TOOLS
    assert app.runtime.config.read_only is False


def test_an_unknown_value_stops_startup_and_is_not_echoed(tmp_path: Path) -> None:
    odd = "maybe-but-with-a-marker-9f3c"
    with pytest.raises(ConfigError) as info:
        load_config(_env(tmp_path / "state", **{ENV_READ_ONLY: odd}))
    assert info.value.code == "bad_read_only"
    assert ENV_READ_ONLY in info.value.message
    assert odd not in info.value.message
    assert odd not in repr(info.value)


def test_read_only_mode_does_not_change_the_read_tools(tmp_path: Path) -> None:
    routes = _version_routes("1.7.2")
    app = build_app(_env(tmp_path / "state", **{ENV_READ_ONLY: "1"}), transport=FakeTransport(routes))
    result = app.tools["inventree_check_version"]()
    assert result["reported_version"] == "1.7.2"
    assert result["writes_allowed"] is False  # no supported range is named, so writes stay refused


def test_startup_line_says_read_only_and_does_not_claim_writes_refused(tmp_path: Path) -> None:
    app = build_app(
        _env(tmp_path / "state", **{ENV_READ_ONLY: "1"}),
        transport=FakeTransport(_version_routes("1.7.2")),
    )
    line = server._startup_line(app.runtime)
    assert "read-only mode, no write planning" in line
    assert "writes refused" not in line
    assert "1.7.2" in line


def test_default_startup_line_keeps_the_write_wording(tmp_path: Path) -> None:
    app = build_app(_env(tmp_path / "state"), transport=FakeTransport(_version_routes("1.7.2")))
    line = server._startup_line(app.runtime)
    assert "writes refused" in line
