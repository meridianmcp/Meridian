"""db03774e -- install.sh must say so up front on platforms with no prebuilt binary.

release.yml only builds ``meridian-connect`` for Linux x86_64, macOS Apple Silicon
and Windows x86_64 (Intel macOS and Linux arm64 were dropped in 80f1d4bc). The
root ``install.sh`` still mapped every ``uname`` result to an asset name, so on an
Intel Mac or a Linux arm64 box it requested a file that does not exist and the
user got a bare ``curl: (22) ... 404`` with no hint of what to do next.

The fix: detect the unsupported platform before any network access, print the
supported alternatives (the ``meridian-server`` PyPI package, or the npm package)
and exit non-zero. ``scripts/install.sh`` (source install) has no binary
download; its only platform-specific failure is ``pixi install`` on Linux arm64
(pixi.toml does not declare linux-aarch64), so the same up-front check guards
just its pixi fallback -- Intel macOS *is* an osx-64 pixi platform and must keep
working there.

Everything runs against fake ``uname``/``curl``/``git``/``pixi``/``uv`` shims, so
no network or real installer is touched.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parent.parent
_ROOT_INSTALL_SH = _REPO / "install.sh"
_SOURCE_INSTALL_SH = _REPO / "scripts" / "install.sh"


def _git_root() -> Path | None:
    git = shutil.which("git")
    if not git:
        return None
    root = Path(git).resolve().parent.parent
    return root if (root / "usr" / "bin").is_dir() else None


def _find_shell(name: str) -> str | None:
    found = shutil.which(name)
    if found and not (os.name == "nt" and "system32" in found.lower()):
        return found
    root = _git_root() if os.name == "nt" else None
    if root is not None:
        for sub in ("bin", "usr/bin"):
            cand = root / sub / f"{name}.exe"
            if cand.exists():
                return str(cand)
    return None


_SH = _find_shell("sh")
_BASH = _find_shell("bash")

pytestmark = [pytest.mark.subprocess_isolated]
_needs_sh = pytest.mark.skipif(_SH is None, reason="no POSIX sh available")
_needs_bash = pytest.mark.skipif(_BASH is None, reason="no bash available")


def _write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))  # never let Windows turn \n into \r\n
    path.chmod(0o755)


def _shell_path(fakebin: Path) -> str:
    parts = [str(fakebin)]
    root = _git_root() if os.name == "nt" else None
    if root is not None:
        parts.append(str(root / "usr" / "bin"))
    parts.append(os.environ.get("PATH", ""))
    return os.pathsep.join(parts)


_FAKE_UNAME = """#!/bin/sh
case "$1" in
  -m) echo "$FAKE_UNAME_M" ;;
  *)  echo "$FAKE_UNAME_S" ;;
esac
"""
_FAKE_UNAME_RUNNER = r"""
uname() {
  case "$1" in
    -m) printf '%s\n' "$FAKE_UNAME_M" ;;
    *)  printf '%s\n' "$FAKE_UNAME_S" ;;
  esac
}
fakebin_path="$(cygpath -u "$1")"
fake_home="$(cygpath -u "$2")"
script_path="$3"
curl_shim_path="$(cygpath -u "$4")"
shift 4
export PATH="$fakebin_path:$PATH"
export HOME="$fake_home"
if [ -n "${MERIDIAN_BIN_DIR:-}" ]; then
  MERIDIAN_BIN_DIR="$(cygpath -u "$MERIDIAN_BIN_DIR")"
  export MERIDIAN_BIN_DIR
fi
if [ -n "${FAKE_CALL_LOG:-}" ]; then
  FAKE_CALL_LOG="$(cygpath -u "$FAKE_CALL_LOG")"
  export FAKE_CALL_LOG
fi
curl() {
  sh "$curl_shim_path" "$@"
}
. "$script_path" "$@"
"""

# Records every invocation; every download "fails" so nothing real can happen.
_FAKE_RECORDER = """#!/bin/sh
echo "$(basename "$0") $*" >> "$FAKE_CALL_LOG"
exit 22
"""

_FAKE_UV_FAIL = """#!/bin/sh
echo "uv $*" >> "$FAKE_CALL_LOG"
exit 1
"""

_FAKE_UV_OK = """#!/bin/sh
echo "uv $*" >> "$FAKE_CALL_LOG"
exit 0
"""

_FAKE_OK = """#!/bin/sh
echo "$(basename "$0") $*" >> "$FAKE_CALL_LOG"
exit 0
"""

# `git clone <repo> <dir>` must leave <dir> behind (the script cd's into it).
_FAKE_GIT = """#!/bin/sh
echo "git $*" >> "$FAKE_CALL_LOG"
if [ "$1" = "clone" ]; then mkdir -p "$3"; fi
exit 0
"""


class _Box:
    def __init__(self, root: Path):
        self.root = root
        self.fakebin = root / "fakebin"
        self.home = root / "home"
        self.bindir = root / "bindir"
        self.log = root / "calls.log"
        self.home.mkdir()
        self.fakebin.mkdir()
        self.log.write_text("", encoding="utf-8")
        _write_lf(self.fakebin / "uname", _FAKE_UNAME)

    def shim(self, name: str, body: str) -> None:
        _write_lf(self.fakebin / name, body)

    def calls(self) -> list[str]:
        return [ln for ln in self.log.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def run(self, shell: str, script: Path, uname_s: str, uname_m: str) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "PATH": _shell_path(self.fakebin),
            "HOME": self.home.as_posix(),
            "MERIDIAN_BIN_DIR": self.bindir.as_posix(),
            "FAKE_UNAME_S": uname_s,
            "FAKE_UNAME_M": uname_m,
            "FAKE_CALL_LOG": self.log.as_posix(),
        }
        env.pop("MERIDIAN_INSTALL_ALLOW_UNVERIFIED", None)
        argv = [shell, script.as_posix()]
        if os.name == "nt":
            # Git Bash/MSYS may search its own uname.exe before a temporary
            # PATH shim. Define uname in the shell process so simulated targets
            # remain deterministic on Windows too.
            argv = [
                shell,
                "-c",
                _FAKE_UNAME_RUNNER,
                "meridian-installer-test",
                self.fakebin.as_posix(),
                self.home.as_posix(),
                script.as_posix(),
                (self.fakebin / "curl").as_posix(),
            ]
        return subprocess.run(
            argv, cwd=self.root, env=env,
            capture_output=True, text=True, timeout=120,
        )


@pytest.fixture
def box(tmp_path):
    return _Box(tmp_path)


def _pypi_name() -> str:
    return tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"]


def _npm_name() -> str:
    return json.loads((_REPO / "npm" / "package.json").read_text(encoding="utf-8"))["name"]


def _published_connect_assets() -> set[str]:
    data = yaml.safe_load((_REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
    include = data["jobs"]["build-connect"]["strategy"]["matrix"]["include"]
    return {row["artifact"] for row in include}


# ---------------------------------------------------------------------------
# root install.sh (the meridian-connect binary installer)
# ---------------------------------------------------------------------------

_UNSUPPORTED = [
    ("Darwin", "x86_64"),   # Intel Mac: no x86_64-apple-darwin asset
    ("Linux", "aarch64"),   # Linux arm64: no aarch64-unknown-linux asset
    ("Linux", "arm64"),
    ("Linux", "riscv64"),
    ("Linux", "i686"),
    ("FreeBSD", "amd64"),
    ("SunOS", "x86_64"),
]


@_needs_sh
@pytest.mark.parametrize("uname_s,uname_m", _UNSUPPORTED)
def test_unsupported_platform_prints_alternatives_and_never_touches_the_network(box, uname_s, uname_m):
    box.shim("curl", _FAKE_RECORDER)
    proc = box.run(_SH, _ROOT_INSTALL_SH, uname_s, uname_m)

    assert proc.returncode != 0
    # Not a bare curl 404: an explanation naming the platform and the way out.
    assert f"{uname_s} {uname_m}" in proc.stderr
    assert "no prebuilt meridian-connect binary" in proc.stderr
    assert f"uv tool install {_pypi_name()}" in proc.stderr
    assert f"pipx install {_pypi_name()}" in proc.stderr
    assert _npm_name() in proc.stderr
    assert "Linux x86_64" in proc.stderr and "Apple Silicon" in proc.stderr
    assert "curl:" not in proc.stderr
    # ... and it must not even attempt the (tag lookup or binary) download.
    assert box.calls() == [], f"unexpected network attempt: {box.calls()}"
    assert not box.bindir.exists(), "nothing may be created before the platform is known to be supported"
    assert proc.stdout.strip() == "", "the message belongs on stderr"


@_needs_sh
def test_windows_shells_are_pointed_at_the_powershell_installer(box):
    box.shim("curl", _FAKE_RECORDER)
    proc = box.run(_SH, _ROOT_INSTALL_SH, "MINGW64_NT-10.0-26200", "x86_64")
    assert proc.returncode != 0
    assert "install.ps1" in proc.stderr
    assert box.calls() == []


@_needs_sh
@pytest.mark.parametrize(
    "uname_s,uname_m,asset",
    [
        ("Linux", "x86_64", "meridian-connect-x86_64-unknown-linux"),
        ("Darwin", "arm64", "meridian-connect-aarch64-apple-darwin"),
        ("Darwin", "aarch64", "meridian-connect-aarch64-apple-darwin"),
    ],
)
def test_supported_platforms_still_request_their_release_asset(box, uname_s, uname_m, asset):
    box.shim("curl", _FAKE_RECORDER)
    proc = box.run(_SH, _ROOT_INSTALL_SH, uname_s, uname_m)
    assert proc.returncode != 0  # the fake curl 404s -- we only care what was requested
    assert "no prebuilt meridian-connect binary" not in proc.stderr
    # (the recorder logs the whole argv, so the URL is followed by "-o <tmpfile>")
    assert any(f"/releases/latest/download/{asset} " in c for c in box.calls()), box.calls()


@_needs_sh
@pytest.mark.parametrize("uname_s,uname_m", [("Linux", "x86_64"), ("Darwin", "arm64"), ("Darwin", "x86_64"), ("Linux", "aarch64")])
def test_gate_agrees_with_what_release_yml_actually_publishes(box, uname_s, uname_m):
    """The supported set in install.sh and the build-connect matrix in release.yml
    must move together: re-adding a platform to the matrix without teaching
    install.sh about it (or the reverse) is exactly how this 404 happened."""
    arch = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64"}[uname_m]
    plat = {"Darwin": "apple-darwin", "Linux": "unknown-linux"}[uname_s]
    asset = f"meridian-connect-{arch}-{plat}"

    box.shim("curl", _FAKE_RECORDER)
    proc = box.run(_SH, _ROOT_INSTALL_SH, uname_s, uname_m)
    attempts_download = any(f"/releases/latest/download/{asset} " in c for c in box.calls())
    blocked = "no prebuilt meridian-connect binary" in proc.stderr

    assert attempts_download != blocked
    assert (asset in _published_connect_assets()) == attempts_download, (
        f"install.sh {'attempts' if attempts_download else 'refuses'} {asset} but "
        f"release.yml {'publishes' if asset in _published_connect_assets() else 'does not publish'} it"
    )


# ---------------------------------------------------------------------------
# scripts/install.sh (source install; uv first, pixi fallback)
# ---------------------------------------------------------------------------

def _pixi_platforms() -> list[str]:
    manifest = tomllib.loads((_REPO / "pixi.toml").read_text(encoding="utf-8"))
    return manifest.get("workspace", manifest.get("project", {}))["platforms"]


def test_pixi_workspace_platforms_match_the_premise_of_the_source_install_gate():
    platforms = _pixi_platforms()
    assert "linux-aarch64" not in platforms, (
        "pixi now declares linux-aarch64: drop the Linux-arm64 gate in scripts/install.sh"
    )
    assert "osx-64" in platforms, "Intel macOS must stay supported by the pixi fallback"


def _source_box(box: _Box, *, uv: str | None) -> _Box:
    box.shim("curl", _FAKE_RECORDER)
    box.shim("git", _FAKE_GIT)
    box.shim("python3", _FAKE_OK)
    box.shim("pixi", _FAKE_OK)
    if uv == "fail":
        box.shim("uv", _FAKE_UV_FAIL)
    elif uv == "ok":
        box.shim("uv", _FAKE_UV_OK)
    return box


@_needs_bash
def test_source_install_on_linux_arm64_without_a_working_uv_fails_up_front(box):
    _source_box(box, uv="fail")
    proc = box.run(_BASH, _SOURCE_INSTALL_SH, "Linux", "aarch64")
    assert proc.returncode != 0
    assert "Linux arm64" in proc.stderr
    assert f"uv tool install {_pypi_name()}" in proc.stderr
    assert f"pip install {_pypi_name()}" in proc.stderr
    # Nothing beyond the (failed) uv attempt: no clone, no pixi bootstrap download.
    assert box.calls() == [f"uv tool install {_pypi_name()}"], box.calls()


@_needs_bash
def test_source_install_on_linux_arm64_still_succeeds_through_uv(box):
    _source_box(box, uv="ok")
    proc = box.run(_BASH, _SOURCE_INSTALL_SH, "Linux", "aarch64")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = box.calls()
    assert calls[0] == f"uv tool install {_pypi_name()}"
    assert calls[1] == (
        "uv tool run --from meridian-server meridian setup --repo "
        + subprocess.run(
            [_BASH, "-c", "pwd -P"], cwd=box.root, capture_output=True,
            text=True, timeout=15,
        ).stdout.strip()
    )
    assert len(calls) == 2


@_needs_bash
@pytest.mark.parametrize("uname_s,uname_m", [("Darwin", "x86_64"), ("Linux", "x86_64"), ("Darwin", "arm64")])
def test_source_install_pixi_fallback_is_not_blocked_on_pixi_platforms(box, uname_s, uname_m):
    """Intel macOS (osx-64) in particular must keep reaching git clone + pixi install."""
    _source_box(box, uv="fail")
    proc = box.run(_BASH, _SOURCE_INSTALL_SH, uname_s, uname_m)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = box.calls()
    assert any(c.startswith("git clone") for c in calls), calls
    assert "pixi install" in calls, calls
    assert any(c.startswith("pixi run python -m meridian setup --repo ") for c in calls), calls
    assert "not available on Linux arm64" not in proc.stderr
