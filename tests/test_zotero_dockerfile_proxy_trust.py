"""Trusted proxy scope in the Dockerfile CMD.

uvicorn rewrites ``request.client`` and the request scheme from ``X-Forwarded-*``
only when the TCP peer is in ``--forwarded-allow-ips``. The Dockerfile CMD picks
that list with a shell condition on ``FLY_APP_NAME``:

* set (hosted on Fly): ``*``. The Fly proxy is the only path in, so hosted
  behaviour is unchanged.
* unset (self-hosted): loopback plus the private ranges where a same-host reverse
  proxy and the Docker gateway sit. A public peer is not trusted.
* set and non-empty (self-hosted only): ``MERIDIAN_FORWARDED_ALLOW_IPS`` replaces
  that default list. The Fly branch ignores it.

These tests read the Dockerfile text, run the CMD's shell script under ``sh`` with
a stub in place of uvicorn (no server starts), and hand the captured list to
uvicorn's own ``ProxyHeadersMiddleware``. Both branches are checked end to end.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCKERFILE = _REPO_ROOT / "Dockerfile"

_SELF_HOSTED_TRUST = ["127.0.0.1", "::1", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"]

_HOSTED_ENV = {"FLY_APP_NAME": "meridian-prod", "MERIDIAN_DB_URL": "postgresql://db.example/meridian"}
_SELF_HOSTED_ENV: dict[str, str] = {}

_XFF = [(b"x-forwarded-for", b"198.51.100.7")]
_PROTO = [(b"x-forwarded-proto", b"https")]


def _cmd_script() -> str:
    """Return the ``sh -c`` script of the Dockerfile CMD, with Docker's line continuations removed."""
    joined = re.sub(r"\\\r?\n", "", _DOCKERFILE.read_text(encoding="utf-8"))
    cmd_lines = [line for line in joined.splitlines() if line.startswith("CMD ")]
    assert len(cmd_lines) == 1, "expected exactly one CMD instruction"
    argv = json.loads(cmd_lines[0][len("CMD "):])
    assert argv[:2] == ["sh", "-c"], "CMD must be the sh -c exec form"
    return argv[2]


def _run_cmd(env_extra: dict[str, str], tmp_path: Path) -> subprocess.CompletedProcess:
    """Run the CMD script with uvicorn replaced by a stub that prints its arguments, one per line."""
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no POSIX sh on this host")
    stub = 'uvicorn() { for arg in "$@"; do printf "%s\\n" "$arg"; done; }\n'
    script = stub + _cmd_script().replace("exec uvicorn ", "uvicorn ", 1)
    script_path = tmp_path / "cmd.sh"
    script_path.write_text(script, encoding="utf-8", newline="\n")
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("FLY_APP_NAME", "MERIDIAN_DB_URL", "MERIDIAN_FORWARDED_ALLOW_IPS")
    }
    env.update(env_extra)
    return subprocess.run(
        [sh, str(script_path)], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )


def _argv(env_extra: dict[str, str], tmp_path: Path) -> list[str]:
    proc = _run_cmd(env_extra, tmp_path)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.splitlines()


def _forwarded_allow_ips(argv: list[str]) -> str:
    matches = [arg for arg in argv if arg.startswith("--forwarded-allow-ips=")]
    assert len(matches) == 1, argv
    return matches[0].split("=", 1)[1]


def _client_after_proxy(trusted: str, peer: str, headers: list[tuple[bytes, bytes]]) -> dict:
    """Feed one request through uvicorn's ProxyHeadersMiddleware and report the client and scheme it sees."""
    seen: dict = {}

    async def app(scope, receive, send):
        seen["client"] = scope["client"]
        seen["scheme"] = scope["scheme"]

    middleware = ProxyHeadersMiddleware(app, trusted_hosts=trusted)
    scope = {"type": "http", "client": (peer, 5555), "scheme": "http", "headers": headers}
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(middleware(scope, None, None))
    finally:
        loop.close()
    return seen


# Both branches of the shell condition, through the real CMD text.

def test_hosted_branch_keeps_the_wildcard_trust(tmp_path):
    argv = _argv(_HOSTED_ENV, tmp_path)
    assert _forwarded_allow_ips(argv) == "*"


def test_self_hosted_branch_trusts_loopback_and_private_ranges_only(tmp_path):
    value = _forwarded_allow_ips(_argv(_SELF_HOSTED_ENV, tmp_path))
    assert "*" not in value.split(",")
    assert value.split(",") == _SELF_HOSTED_TRUST
    for entry in value.split(","):
        network = ipaddress.ip_network(entry, strict=False)
        assert network.is_loopback or network.is_private, entry


def test_both_branches_keep_proxy_headers_and_the_server_target(tmp_path):
    for env in (_HOSTED_ENV, _SELF_HOSTED_ENV):
        argv = _argv(env, tmp_path)
        assert "--proxy-headers" in argv
        assert "meridian.server:app" in argv
        assert argv[argv.index("--host") + 1] == "0.0.0.0"
        assert argv[argv.index("--port") + 1] == "8000"


def test_hosted_branch_still_refuses_to_start_without_a_database_url(tmp_path):
    proc = _run_cmd({"FLY_APP_NAME": "meridian-prod"}, tmp_path)
    assert proc.returncode == 1
    assert "MERIDIAN_DB_URL" in proc.stdout + proc.stderr


# What uvicorn does with each trusted list, using uvicorn's own middleware.

@pytest.mark.parametrize(
    "peer",
    [
        "172.17.0.1",   # default Docker bridge gateway, where a host reverse proxy arrives
        "192.168.65.1", # Docker Desktop host gateway
        "127.0.0.1",
        "::1",
    ],
)
def test_self_hosted_honours_forwarding_from_loopback_and_the_docker_gateway(tmp_path, peer):
    trusted = _forwarded_allow_ips(_argv(_SELF_HOSTED_ENV, tmp_path))
    seen = _client_after_proxy(trusted, peer, _XFF + _PROTO)
    assert seen["client"][0] == "198.51.100.7"
    assert seen["scheme"] == "https"


@pytest.mark.parametrize("peer", ["203.0.113.9", "198.51.100.20"])
def test_self_hosted_ignores_forwarding_from_a_public_peer(tmp_path, peer):
    trusted = _forwarded_allow_ips(_argv(_SELF_HOSTED_ENV, tmp_path))
    seen = _client_after_proxy(trusted, peer, _XFF + _PROTO)
    assert seen["client"] == (peer, 5555)
    assert seen["scheme"] == "http"


def test_hosted_still_honours_forwarding_from_any_peer(tmp_path):
    trusted = _forwarded_allow_ips(_argv(_HOSTED_ENV, tmp_path))
    seen = _client_after_proxy(trusted, "203.0.113.9", _XFF)
    assert seen["client"][0] == "198.51.100.7"


def test_the_cmd_script_is_valid_posix_sh(tmp_path):
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no POSIX sh on this host")
    script_path = tmp_path / "cmd.sh"
    script_path.write_text(_cmd_script(), encoding="utf-8", newline="\n")
    proc = subprocess.run([sh, "-n", str(script_path)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr


# Operator override: MERIDIAN_FORWARDED_ALLOW_IPS replaces the self-hosted default list.

_OVERRIDE_KEY = "MERIDIAN_FORWARDED_ALLOW_IPS"
_PUBLIC_PROXY = "203.0.113.9"          # stands in for a reverse proxy on another public host
_PUBLIC_PROXY_RANGE = "203.0.113.0/24"


@pytest.mark.parametrize("value", [_PUBLIC_PROXY_RANGE, "203.0.113.9, 2001:db8::/32", "cmd.*"])
def test_self_hosted_override_is_passed_through_verbatim(tmp_path, value):
    # The script file is cmd.sh in tmp_path. An unquoted cmd.* would glob-expand to cmd.sh,
    # and an unquoted value with a space would split into two words, so both prove the quoting.
    argv = _argv({_OVERRIDE_KEY: value}, tmp_path)
    assert _forwarded_allow_ips(argv) == value


@pytest.mark.parametrize("env", [{}, {_OVERRIDE_KEY: ""}], ids=["unset", "empty"])
def test_self_hosted_unset_or_empty_override_falls_back_to_the_default(tmp_path, env):
    assert _forwarded_allow_ips(_argv(env, tmp_path)).split(",") == _SELF_HOSTED_TRUST


def test_hosted_branch_ignores_the_override(tmp_path):
    argv = _argv({**_HOSTED_ENV, _OVERRIDE_KEY: _PUBLIC_PROXY_RANGE}, tmp_path)
    assert _forwarded_allow_ips(argv) == "*"


@pytest.mark.parametrize("peer", ["127.0.0.1", "172.17.0.1"])
def test_override_replaces_the_default_so_loopback_and_docker_gateway_are_no_longer_trusted(tmp_path, peer):
    trusted = _forwarded_allow_ips(_argv({_OVERRIDE_KEY: _PUBLIC_PROXY}, tmp_path))
    seen = _client_after_proxy(trusted, peer, _XFF + _PROTO)
    assert seen["client"] == (peer, 5555)
    assert seen["scheme"] == "http"


@pytest.mark.parametrize(
    ("override", "peer"),
    [(_PUBLIC_PROXY, _PUBLIC_PROXY), (_PUBLIC_PROXY_RANGE, "203.0.113.200")],
    ids=["address", "range"],
)
def test_override_honours_forwarding_from_the_named_proxy_address_or_range(tmp_path, override, peer):
    trusted = _forwarded_allow_ips(_argv({_OVERRIDE_KEY: override}, tmp_path))
    seen = _client_after_proxy(trusted, peer, _XFF + _PROTO)
    assert seen["client"][0] == "198.51.100.7"
    assert seen["scheme"] == "https"
