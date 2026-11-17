"""Per-host label for the permanent hooks-installer token.

Before this change every machine's installer sent the same label,
"hooks-installer". POST /auth/tokens deletes every token that carries the
label before it mints the new one (the label is a unique slot), so installing
on a second machine revoked the first machine's permanent token.

Covered here:
  * the installer side: scripts/meridian_connect.py ``_hooks_token_label`` and
    the label ``main()`` actually posts to /auth/tokens;
  * the server side: the real POST /auth/tokens handler
    (``srv.create_api_token``) run against an in-memory database, so the
    "delete only the exact label" behaviour is exercised rather than assumed.

Nothing here touches the network, a subprocess or a real home directory: the
installer's ``_http`` is faked and HOME is redirected into ``tmp_path``.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import shutil
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import meridian.server as srv  # import first -- avoids handler/server import cycle
from meridian import db as db_module

_CONNECT_PY = Path(__file__).resolve().parent.parent / "scripts" / "meridian_connect.py"
_URL = "https://meridian.example.test"
_FAKE_TOKEN = "sk_meridian_faketoken_not_real_host_label"  # noqa: S105 -- fixture value


def _load_connect():
    spec = importlib.util.spec_from_file_location("meridian_connect_host_label", _CONNECT_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _set_hostname(monkeypatch, mod, name):
    monkeypatch.setattr(mod.socket, "gethostname", lambda: name)


def _break_hostname(monkeypatch, mod):
    def boom():
        raise OSError("no hostname")

    monkeypatch.setattr(mod.socket, "gethostname", boom)


def _label_for(monkeypatch, mod, hostname):
    _set_hostname(monkeypatch, mod, hostname)
    return mod._hooks_token_label()


# ---------------------------------------------------------------------------
# Installer: the label itself
# ---------------------------------------------------------------------------

class TestHooksTokenLabel:
    def test_label_carries_the_hostname(self, monkeypatch):
        mod = _load_connect()
        assert _label_for(monkeypatch, mod, "laptop-a") == "hooks-installer-laptop-a"

    def test_two_hostnames_get_two_different_labels(self, monkeypatch):
        mod = _load_connect()
        assert _label_for(monkeypatch, mod, "laptop-a") != _label_for(monkeypatch, mod, "desktop-b")

    def test_unsafe_characters_are_dropped(self, monkeypatch):
        mod = _load_connect()
        assert _label_for(monkeypatch, mod, "a b/c'd\"e") == "hooks-installer-abcde"

    def test_allowed_punctuation_is_kept(self, monkeypatch):
        mod = _load_connect()
        assert _label_for(monkeypatch, mod, "laptop-A.local_1") == "hooks-installer-laptop-A.local_1"

    def test_hostname_is_capped_at_200_characters(self, monkeypatch):
        mod = _load_connect()
        assert _label_for(monkeypatch, mod, "a" * 500) == "hooks-installer-" + "a" * 200

    def test_unreadable_hostname_falls_back_to_the_legacy_label(self, monkeypatch):
        mod = _load_connect()
        _break_hostname(monkeypatch, mod)
        assert mod._hooks_token_label() == "hooks-installer"

    @pytest.mark.parametrize("raw", ["", "!!!", " \t "])
    def test_hostname_that_sanitizes_to_nothing_falls_back(self, monkeypatch, raw):
        mod = _load_connect()
        assert _label_for(monkeypatch, mod, raw) == "hooks-installer"

    @pytest.mark.parametrize(
        "raw", ["laptop-a", "a b/c'd\"e", "laptop-A.local_1", "x" * 500, "!!!", ""]
    )
    def test_sanitizer_matches_the_server_tunnel_hostname_rule(self, monkeypatch, raw):
        """Pin the installer's copy of the rule to server.py's real one."""
        mod = _load_connect()
        cleaned = srv._sanitize_tunnel_hostname(raw)
        expected = f"hooks-installer-{cleaned}" if cleaned else "hooks-installer"
        assert _label_for(monkeypatch, mod, raw) == expected


# ---------------------------------------------------------------------------
# Installer: what main() posts to /auth/tokens
# ---------------------------------------------------------------------------

@pytest.fixture()
def install_run(tmp_path, monkeypatch):
    """Run meridian_connect.main() against a faked network and a temp home.

    Returns (mod, posts, monkeypatch); ``posts`` collects every body POSTed to
    /auth/tokens.
    """
    home = tmp_path / "home"
    (home / "AppData" / "Roaming").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    for name in ("MERIDIAN_TOKEN", "MERIDIAN_API_KEY", "BEARER_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)

    mod = _load_connect()
    posts: list[dict] = []

    def fake_http(method, url, *, token="", body=None, timeout=10):
        if url.endswith("/auth/tokens"):
            posts.append(body)
            return {"token": "sk_meridian_permanent_fixture"}
        if url.endswith("/auth/me"):
            return {"email": "tester@example.test"}
        return {}

    real_which = shutil.which

    def fake_which(cmd, *a, **k):
        # No Claude Code / Codex / Cursor: keep the test away from real CLIs.
        if cmd in ("claude", "codex", "cursor"):
            return None
        return real_which(cmd, *a, **k)

    monkeypatch.setattr(mod, "_http", fake_http)
    monkeypatch.setattr(mod.shutil, "which", fake_which)
    return mod, posts, monkeypatch


def _run_install(mod, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["meridian_connect.py", "--url", _URL, "--token", _FAKE_TOKEN])
    return mod.main()


def test_install_posts_the_per_host_label(install_run):
    mod, posts, monkeypatch = install_run
    _set_hostname(monkeypatch, mod, "laptop-a")
    assert _run_install(mod, monkeypatch) == 0
    assert posts == [{"label": "hooks-installer-laptop-a"}]


def test_install_with_no_usable_hostname_posts_the_legacy_label(install_run):
    mod, posts, monkeypatch = install_run
    # An empty hostname (not an exception) keeps main()'s later smoke-test
    # gethostname() call harmless, while still exercising the fallback.
    _set_hostname(monkeypatch, mod, "")
    assert _run_install(mod, monkeypatch) == 0
    assert posts == [{"label": "hooks-installer"}]


# ---------------------------------------------------------------------------
# Server: the real POST /auth/tokens handler against an in-memory database
# ---------------------------------------------------------------------------

def _fake_request(db, body):
    async def _json():
        return body

    return types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db)),
        json=_json,
    )


async def _tenant_db(monkeypatch):
    db = await db_module.init_db(":memory:")
    tenant = await db_module.upsert_tenant(db, "a@x.com")
    monkeypatch.setattr(srv, "_get_authenticated_tenant", AsyncMock(return_value=tenant))
    return db, tenant


async def _mint(db, label):
    """Run the real POST /auth/tokens handler and return its response body."""
    return await srv.create_api_token(_fake_request(db, {"label": label}))


async def _rows(db, tenant):
    return await db_module.list_api_tokens(db, tenant["id"])


async def _authenticates(db, raw_token):
    digest = hashlib.sha256(raw_token.encode()).hexdigest()
    return (await db_module.get_tenant_from_token_hash(db, digest)) is not None


def test_two_hostnames_do_not_revoke_each_other(monkeypatch):
    """The reported bug: a second machine's install revoked the first's token."""
    mod = _load_connect()
    label_a = _label_for(monkeypatch, mod, "laptop-a")
    label_b = _label_for(monkeypatch, mod, "desktop-b")

    async def _run():
        db, tenant = await _tenant_db(monkeypatch)
        a = await _mint(db, label_a)
        b = await _mint(db, label_b)
        return (
            a,
            b,
            await _rows(db, tenant),
            await _authenticates(db, a["token"]),
            await _authenticates(db, b["token"]),
        )

    a, b, rows, a_ok, b_ok = asyncio.run(_run())
    assert {r["id"] for r in rows} == {a["id"], b["id"]}
    assert a_ok, "laptop-a's token must still authenticate after desktop-b installs"
    assert b_ok


def test_reinstalling_on_one_host_replaces_only_that_hosts_token(monkeypatch):
    mod = _load_connect()
    label_a = _label_for(monkeypatch, mod, "laptop-a")
    label_b = _label_for(monkeypatch, mod, "desktop-b")

    async def _run():
        db, tenant = await _tenant_db(monkeypatch)
        b_first = await _mint(db, label_b)
        a_first = await _mint(db, label_a)
        a_again = await _mint(db, label_a)  # second install on laptop-a
        return (
            b_first,
            a_first,
            a_again,
            await _rows(db, tenant),
            await _authenticates(db, a_first["token"]),
            await _authenticates(db, b_first["token"]),
        )

    b_first, a_first, a_again, rows, a_old_ok, b_ok = asyncio.run(_run())
    assert a_again["id"] != a_first["id"]
    assert {r["id"] for r in rows} == {b_first["id"], a_again["id"]}
    assert not a_old_ok, "laptop-a's superseded token is replaced by its own reinstall"
    assert b_ok, "desktop-b's token is untouched by laptop-a's reinstall"


def test_legacy_shared_token_keeps_working_after_a_per_host_install(monkeypatch):
    """Upgrade path: a token minted by an older installer (label
    "hooks-installer") is not deleted when a per-host install happens."""
    mod = _load_connect()
    label_a = _label_for(monkeypatch, mod, "laptop-a")

    async def _run():
        db, tenant = await _tenant_db(monkeypatch)
        legacy = await _mint(db, "hooks-installer")
        await _mint(db, label_a)
        return legacy, await _rows(db, tenant), await _authenticates(db, legacy["token"])

    legacy, rows, legacy_ok = asyncio.run(_run())
    assert legacy["id"] in {r["id"] for r in rows}
    assert legacy_ok


def test_fallback_install_deletes_only_the_exact_legacy_label(monkeypatch):
    """A machine with no usable hostname uses the legacy label; that install
    may replace other legacy-label tokens but must never touch a per-host one."""
    mod = _load_connect()
    label_a = _label_for(monkeypatch, mod, "laptop-a")
    fallback = _label_for(monkeypatch, mod, "!!!")
    assert fallback == "hooks-installer"

    async def _run():
        db, tenant = await _tenant_db(monkeypatch)
        a = await _mint(db, label_a)
        await _mint(db, fallback)
        fallback_again = await _mint(db, fallback)
        return a, fallback_again, await _rows(db, tenant), await _authenticates(db, a["token"])

    a, fallback_again, rows, a_ok = asyncio.run(_run())
    assert {r["id"] for r in rows} == {a["id"], fallback_again["id"]}
    assert a_ok


def test_capped_per_host_label_is_accepted_and_stored_verbatim(monkeypatch):
    mod = _load_connect()
    label = _label_for(monkeypatch, mod, "h" * 500)
    assert label == "hooks-installer-" + "h" * 200

    async def _run():
        db, tenant = await _tenant_db(monkeypatch)
        minted = await _mint(db, label)
        return minted, await _rows(db, tenant)

    minted, rows = asyncio.run(_run())
    assert minted["label"] == label
    assert [r["label"] for r in rows] == [label]
