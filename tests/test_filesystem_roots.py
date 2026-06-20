"""Tests for configurable filesystem connector roots (executor_config.filesystem_roots).

Covers: the executor_config schema + DB save/load round-trip, the per-tenant
union helper used by GET /tunnel/filesystem-roots, and the tunnel client's
filesystem-server command builder (multi-root vs home-dir fallback).
"""
from __future__ import annotations

import asyncio
import json

from meridian import db as db_module
from meridian import tunnel_client as tc
from meridian.executor_config import normalize_executor_config
from meridian.routes import tunnel as tn


# ---------------------------------------------------------------------------
# Schema + DB round-trip
# ---------------------------------------------------------------------------

def test_normalize_executor_config_keeps_filesystem_roots():
    out = normalize_executor_config({"filesystem_roots": ["/x", "/y"], "bogus": 1})
    assert out["filesystem_roots"] == ["/x", "/y"]
    assert "bogus" not in out


def test_executor_config_filesystem_roots_roundtrip():
    async def _run():
        db = await db_module.init_db(":memory:")
        proj = await db_module.create_project(db, "fs-roots-roundtrip")
        await db_module.set_executor_config(
            db, proj["id"],
            {"filesystem_roots": ["C:/Users/me/Documents", "D:/Projects"], "repo_paths": []},
        )
        return await db_module.get_executor_config(db, proj["id"])

    cfg = asyncio.run(_run())
    assert cfg["filesystem_roots"] == ["C:/Users/me/Documents", "D:/Projects"]


# ---------------------------------------------------------------------------
# Per-tenant union (GET /tunnel/filesystem-roots helper)
# ---------------------------------------------------------------------------

def test_union_filesystem_roots_dedupes_and_parses():
    projects = [
        {"executor_config": json.dumps({"filesystem_roots": ["/a", "/b"]})},
        {"executor_config": {"filesystem_roots": ["/b", "/c"]}},  # already a dict
        {"executor_config": None},                                 # no config
        {"executor_config": "not json at all"},                    # malformed
        {"executor_config": json.dumps({"filesystem_roots": [" ", 5, "/d "]})},  # junk + trim
    ]
    assert tn._union_filesystem_roots(projects) == ["/a", "/b", "/c", "/d"]


def test_union_filesystem_roots_empty_when_none_set():
    assert tn._union_filesystem_roots([{"executor_config": json.dumps({"repo_paths": []})}]) == []
    assert tn._union_filesystem_roots([]) == []


# ---------------------------------------------------------------------------
# Filesystem-server command builder (multi-root vs home-dir fallback)
# ---------------------------------------------------------------------------

def test_build_proxy_command_uses_roots_when_provided():
    cmd = tc._build_proxy_command("npx", "/home/me", 8808, roots=["/a", "/b"])
    assert "@modelcontextprotocol/server-filesystem" in cmd
    # Both roots are appended as the served dirs.
    sep = cmd.index("--")
    assert cmd[sep + 1:] == ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/a", "/b"]


def test_build_proxy_command_falls_back_to_repo_path():
    cmd = tc._build_proxy_command("npx", "/home/me", 8808, roots=[])
    assert cmd[-1] == "/home/me"
    # None behaves the same (default unchanged).
    assert tc._build_proxy_command("npx", "/home/me", 8808)[-1] == "/home/me"


def test_build_proxy_command_ignores_blank_roots():
    cmd = tc._build_proxy_command("npx", "/home/me", 8808, roots=["", "  "])
    assert cmd[-1] == "/home/me"
