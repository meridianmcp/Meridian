"""f66e8f23 -- installers and the tunnel must verify what they download.

Found by the 2026-09-27 install/tunnel audit: install.sh, install.ps1 and
scripts/install-windows.ps1 downloaded a binary from the GitHub release and ran it
with no integrity check, release.yml published no checksums, and the tunnel
downloaded the *latest* DeusData/codebase-memory-mcp release, picked an asset by
filename heuristics and ran it -- also unchecked.

The fix covered here:

- release.yml generates ``SHA256SUMS`` over every uploaded binary and attaches it
  to the release.
- The three installers fetch it and verify the download BEFORE running or
  installing it; a mismatch, a missing SHA256SUMS or a missing entry deletes the
  download and aborts non-zero (fail closed). The only escape hatch is the
  explicit, loudly-warned ``MERIDIAN_INSTALL_ALLOW_UNVERIFIED=1``.
- ``meridian.tunnel_client`` downloads one *pinned* codebase-memory-mcp release
  asset per platform and refuses to extract or install it unless its SHA-256
  equals the pin (fail closed).

Nothing here needs the network: the shell installer is driven through fake
``curl``/``uname`` shims, the PowerShell helpers through a loopback HTTP server,
and the tunnel through a faked ``httpx.AsyncClient``.
"""
from __future__ import annotations

import asyncio
import hashlib
import http.server
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import zipfile
from pathlib import Path

import pytest
import yaml

from meridian import tunnel_client as tc

_REPO = Path(__file__).resolve().parent.parent
_INSTALL_SH = _REPO / "install.sh"
_INSTALL_PS1 = _REPO / "install.ps1"
_INSTALL_WINDOWS_PS1 = _REPO / "scripts" / "install-windows.ps1"
_RELEASE_YML = _REPO / ".github" / "workflows" / "release.yml"

_OPT_OUT = "MERIDIAN_INSTALL_ALLOW_UNVERIFIED"


# ---------------------------------------------------------------------------
# shell discovery (never pick the Windows "bash.exe" WSL launcher)
# ---------------------------------------------------------------------------

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
_POWERSHELL = shutil.which("powershell")

_needs_sh = pytest.mark.skipif(_SH is None, reason="no POSIX sh available")
_needs_bash = pytest.mark.skipif(_BASH is None, reason="no bash available")
_needs_ps51 = pytest.mark.skipif(_POWERSHELL is None, reason="Windows PowerShell 5.1 not available")


def _write_lf(path: Path, text: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))  # never let Windows turn \n into \r\n
    if executable:
        path.chmod(0o755)


def _shell_path(fakebin: Path) -> str:
    parts = [str(fakebin)]
    root = _git_root() if os.name == "nt" else None
    if root is not None:
        parts.append(str(root / "usr" / "bin"))
    parts.append(os.environ.get("PATH", ""))
    return os.pathsep.join(parts)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# release.yml publishes SHA256SUMS
# ---------------------------------------------------------------------------

def _release_job() -> dict:
    data = yaml.safe_load(_RELEASE_YML.read_text(encoding="utf-8"))
    return data["jobs"]["release"]


def _sums_step_and_release_step() -> tuple[int, dict, int, dict]:
    steps = _release_job()["steps"]
    sums_idx = next(i for i, s in enumerate(steps) if "SHA256SUMS" in s.get("name", ""))
    rel_idx = next(i for i, s in enumerate(steps) if s.get("name") == "Create GitHub Release")
    return sums_idx, steps[sums_idx], rel_idx, steps[rel_idx]


def _release_files() -> list[str]:
    _, _, _, rel = _sums_step_and_release_step()
    return [ln.strip() for ln in rel["with"]["files"].splitlines() if ln.strip()]


def test_release_workflow_generates_sha256sums_before_the_release_step():
    sums_idx, sums_step, rel_idx, _ = _sums_step_and_release_step()
    assert sums_idx < rel_idx, "SHA256SUMS must exist before the release step uploads it"
    assert "sha256sum" in sums_step["run"]
    assert "release-assets/SHA256SUMS" in sums_step["run"]
    assert sums_step.get("continue-on-error") not in (True, "true")


def test_release_uploads_sha256sums_alongside_the_binaries():
    files = _release_files()
    assert "release-assets/SHA256SUMS" in files


def test_every_uploaded_binary_is_covered_by_the_checksum_step():
    """The checksum list and the upload list are maintained by hand in two places;
    a binary uploaded without a checksum would make every installer refuse it."""
    _, sums_step, _, _ = _sums_step_and_release_step()
    binaries = [f for f in _release_files() if f != "release-assets/SHA256SUMS"]
    assert binaries, "release.yml no longer uploads any binaries?"
    for path in binaries:
        assert path in sums_step["run"], f"{path} is uploaded but not checksummed"


def test_checksum_step_covers_every_asset_the_installers_download():
    _, sums_step, _, _ = _sums_step_and_release_step()
    for asset in (
        "meridian-connect-x86_64-unknown-linux",
        "meridian-connect-aarch64-apple-darwin",
        "meridian-connect-x86_64-windows.exe",
        "meridian.exe",
        "meridian-tray.exe",
    ):
        assert f"/{asset}" in sums_step["run"], asset


@pytest.mark.subprocess_isolated
@_needs_bash
def test_checksum_step_script_produces_sha256sum_format_and_skips_missing(tmp_path):
    """Run the real step script against fake artifacts: hashes are correct, names are
    bare basenames (so `sha256sum -c` and the installers' name lookup both work), and
    an artifact that did not build is skipped rather than failing the release."""
    _, sums_step, _, _ = _sums_step_and_release_step()
    present = {
        "dist-artifacts/meridian-windows/meridian.exe": b"windows-binary",
        "dist-artifacts/meridian-connect-x86_64-unknown-linux/meridian-connect-x86_64-unknown-linux": b"linux-connect",
    }
    for rel, content in present.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    script = tmp_path / "step.sh"
    _write_lf(script, sums_step["run"])
    proc = subprocess.run(
        [_BASH, script.as_posix()], cwd=tmp_path, capture_output=True, text=True, timeout=60,
        env={**os.environ, "PATH": _shell_path(tmp_path / "nobin")},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = (tmp_path / "release-assets" / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    parsed = {}
    for ln in lines:
        # "<hex>  <name>" (text mode, GNU/Linux) or "<hex> *<name>" (binary mode,
        # the Git-for-Windows default) -- the installers accept both.
        m = re.fullmatch(r"([0-9a-f]{64}) [ *](\S+)", ln)
        assert m, f"not sha256sum format: {ln!r}"
        parsed[m.group(2)] = m.group(1)
    assert parsed == {
        "meridian.exe": _sha256(b"windows-binary"),
        "meridian-connect-x86_64-unknown-linux": _sha256(b"linux-connect"),
    }


@pytest.mark.subprocess_isolated
@_needs_bash
def test_checksum_step_script_fails_when_nothing_was_built(tmp_path):
    _, sums_step, _, _ = _sums_step_and_release_step()
    script = tmp_path / "step.sh"
    _write_lf(script, sums_step["run"])
    proc = subprocess.run(
        [_BASH, script.as_posix()], cwd=tmp_path, capture_output=True, text=True, timeout=60,
        env={**os.environ, "PATH": _shell_path(tmp_path / "nobin")},
    )
    assert proc.returncode != 0, "an empty SHA256SUMS must fail the release, not ship silently"


# ---------------------------------------------------------------------------
# install.sh: fake curl / uname sandbox
# ---------------------------------------------------------------------------

_FAKE_UNAME = """#!/bin/sh
case "$1" in
  -m) echo "$FAKE_UNAME_M" ;;
  *)  echo "$FAKE_UNAME_S" ;;
esac
"""

# Serves files out of $FAKE_SERVE by URL basename; api.github.com answers with a tag.
_FAKE_CURL = """#!/bin/sh
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -*) shift ;;
    *)  url="$1"; shift ;;
  esac
done
echo "$url" >> "$FAKE_CURL_LOG"
case "$url" in
  https://api.github.com/*)
    printf '{"tag_name": "%s"}\\n' "$FAKE_TAG"
    exit 0 ;;
  */SHA256SUMS|*/meridian-connect-*)
    f="$FAKE_SERVE/$(basename "$url")"
    [ -f "$f" ] || exit 22
    if [ -n "$out" ]; then cp "$f" "$out"; else cat "$f"; fi
    exit 0 ;;
esac
exit 22
"""

_FAKE_CONNECT = """#!/bin/sh
echo "FAKE-CONNECT-RAN $*"
"""

_LINUX_ASSET = "meridian-connect-x86_64-unknown-linux"


class _Sandbox:
    def __init__(self, root: Path):
        self.root = root
        self.fakebin = root / "fakebin"
        self.serve = root / "serve"
        self.bindir = root / "bindir"
        self.home = root / "home"
        self.log = root / "curl.log"
        for d in (self.fakebin, self.serve, self.home):
            d.mkdir(parents=True, exist_ok=True)
        _write_lf(self.fakebin / "uname", _FAKE_UNAME, executable=True)
        _write_lf(self.fakebin / "curl", _FAKE_CURL, executable=True)
        self.log.write_text("", encoding="utf-8")

    def publish(self, name: str, content: str | bytes) -> bytes:
        data = content.encode("utf-8") if isinstance(content, str) else content
        (self.serve / name).write_bytes(data)
        return data

    def curl_urls(self) -> list[str]:
        return [ln for ln in self.log.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def run(self, *args: str, uname_s: str = "Linux", uname_m: str = "x86_64", **extra_env: str):
        env = {
            **os.environ,
            "PATH": _shell_path(self.fakebin),
            "HOME": self.home.as_posix(),
            "MERIDIAN_BIN_DIR": self.bindir.as_posix(),
            "FAKE_UNAME_S": uname_s,
            "FAKE_UNAME_M": uname_m,
            "FAKE_SERVE": self.serve.as_posix(),
            "FAKE_CURL_LOG": self.log.as_posix(),
            "FAKE_TAG": "v9.9.9",
        }
        env.pop(_OPT_OUT, None)
        env.update(extra_env)
        return subprocess.run(
            [_SH, _INSTALL_SH.as_posix(), *args],
            cwd=self.root, env=env, capture_output=True, text=True, timeout=120,
        )

    @property
    def dest(self) -> Path:
        return self.bindir / "meridian-connect"


@pytest.fixture
def sandbox(tmp_path):
    return _Sandbox(tmp_path)


def _publish_release(sb: _Sandbox, *, sums: str | None = "auto") -> bytes:
    """Publish the fake Linux connect binary, and (by default) a correct SHA256SUMS."""
    binary = sb.publish(_LINUX_ASSET, _FAKE_CONNECT)
    if sums == "auto":
        sb.publish("SHA256SUMS", f"{_sha256(binary)}  {_LINUX_ASSET}\n")
    elif sums is not None:
        sb.publish("SHA256SUMS", sums)
    return binary


def _leftovers(sb: _Sandbox) -> list[str]:
    return sorted(p.name for p in sb.bindir.iterdir()) if sb.bindir.exists() else []


@pytest.mark.subprocess_isolated
@_needs_sh
def test_install_sh_installs_and_runs_a_verified_binary(sandbox):
    _publish_release(sandbox)
    proc = sandbox.run("--some-flag")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Checksum verified" in proc.stdout
    assert "FAKE-CONNECT-RAN --some-flag" in proc.stdout, "args must still be forwarded"
    assert sandbox.dest.exists()
    assert _leftovers(sandbox) == ["meridian-connect"], "no temp files may be left behind"


@pytest.mark.subprocess_isolated
@_needs_sh
def test_install_sh_accepts_binary_mode_star_entries_and_uppercase_hashes(sandbox):
    binary = sandbox.publish(_LINUX_ASSET, _FAKE_CONNECT)
    sandbox.publish("SHA256SUMS", f"{_sha256(binary).upper()} *{_LINUX_ASSET}\n")
    proc = sandbox.run()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "FAKE-CONNECT-RAN" in proc.stdout


@pytest.mark.subprocess_isolated
@_needs_sh
def test_install_sh_checksum_mismatch_aborts_deletes_and_never_runs(sandbox):
    sandbox.publish(_LINUX_ASSET, _FAKE_CONNECT)
    sandbox.publish("SHA256SUMS", f"{'0' * 64}  {_LINUX_ASSET}\n")
    proc = sandbox.run()
    assert proc.returncode != 0
    assert "MISMATCH" in proc.stderr
    assert "FAKE-CONNECT-RAN" not in proc.stdout, "an unverified binary must never be executed"
    assert not sandbox.dest.exists()
    assert _leftovers(sandbox) == [], "the downloaded file must be deleted on mismatch"


@pytest.mark.subprocess_isolated
@_needs_sh
def test_install_sh_tampered_binary_with_genuine_sums_is_rejected(sandbox):
    """The realistic attack: the checksum file is the genuine one, the binary was swapped."""
    genuine = _FAKE_CONNECT.encode("utf-8")
    sandbox.publish("SHA256SUMS", f"{_sha256(genuine)}  {_LINUX_ASSET}\n")
    sandbox.publish(_LINUX_ASSET, "#!/bin/sh\necho EVIL-RAN\n")
    proc = sandbox.run()
    assert proc.returncode != 0
    assert "EVIL-RAN" not in proc.stdout
    assert _leftovers(sandbox) == []


@pytest.mark.subprocess_isolated
@_needs_sh
def test_install_sh_missing_sha256sums_fails_closed(sandbox):
    _publish_release(sandbox, sums=None)  # a release that predates SHA256SUMS
    proc = sandbox.run()
    assert proc.returncode != 0
    assert "SHA256SUMS" in proc.stderr
    assert _OPT_OUT in proc.stderr, "the error must tell the user about the explicit opt-out"
    assert "FAKE-CONNECT-RAN" not in proc.stdout
    assert _leftovers(sandbox) == []


@pytest.mark.subprocess_isolated
@_needs_sh
def test_install_sh_sums_without_an_entry_for_the_asset_fails_closed(sandbox):
    _publish_release(sandbox, sums=f"{'a' * 64}  meridian-connect-aarch64-apple-darwin\n")
    proc = sandbox.run()
    assert proc.returncode != 0
    assert "FAKE-CONNECT-RAN" not in proc.stdout
    assert _leftovers(sandbox) == []


@pytest.mark.subprocess_isolated
@_needs_sh
def test_install_sh_malformed_sums_entry_fails_closed(sandbox):
    _publish_release(sandbox, sums=f"nothex  {_LINUX_ASSET}\n")
    proc = sandbox.run()
    assert proc.returncode != 0
    assert "FAKE-CONNECT-RAN" not in proc.stdout


@pytest.mark.subprocess_isolated
@_needs_sh
def test_install_sh_explicit_opt_out_installs_with_a_loud_warning(sandbox):
    _publish_release(sandbox, sums=None)
    proc = sandbox.run(**{_OPT_OUT: "1"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "WARNING" in proc.stderr and "WITHOUT" in proc.stderr
    assert "FAKE-CONNECT-RAN" in proc.stdout
    assert not any(u.endswith("/SHA256SUMS") for u in sandbox.curl_urls()), "opt-out must not even fetch it"


@pytest.mark.subprocess_isolated
@_needs_sh
def test_install_sh_opt_out_must_be_exactly_one(sandbox):
    _publish_release(sandbox, sums=None)
    for value in ("0", "true", "yes", ""):
        proc = sandbox.run(**{_OPT_OUT: value})
        assert proc.returncode != 0, f"{_OPT_OUT}={value!r} must not disable verification"


@pytest.mark.subprocess_isolated
@_needs_sh
def test_install_sh_download_failure_still_fails(sandbox):
    # Nothing published at all: the binary download itself 404s.
    proc = sandbox.run()
    assert proc.returncode != 0
    assert not sandbox.dest.exists()
    assert _leftovers(sandbox) == []


def test_install_sh_source_keeps_the_release_version_lookup():
    """Regression guard for 50d2664d (tests/test_install_script.py): still no jq."""
    src = _INSTALL_SH.read_text(encoding="utf-8")
    assert "sha256sum" in src and "shasum -a 256" in src
    assert "SHA256SUMS" in src
    assert _OPT_OUT in src
    assert "| jq" not in src


# ---------------------------------------------------------------------------
# PowerShell installers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("script", [_INSTALL_PS1, _INSTALL_WINDOWS_PS1], ids=lambda p: p.name)
def test_powershell_installers_reference_the_verification_contract(script):
    src = script.read_text(encoding="utf-8")
    assert "SHA256SUMS" in src
    assert "Get-FileHash" in src
    assert "Test-MeridianDownloadIntegrity" in src
    assert _OPT_OUT in src
    assert "releases/latest/download/SHA256SUMS" in src


def test_install_ps1_verifies_before_running_or_touching_path():
    src = _INSTALL_PS1.read_text(encoding="utf-8")
    call = src.index("if (-not (Test-MeridianDownloadIntegrity -Path $dest")
    assert src.index("if (-not $downloaded)") < call
    assert call < src.index("SetEnvironmentVariable"), "PATH must not be updated before verification"
    assert call < src.index("& $dest @binaryArgs"), "the binary must not run before verification"
    # A failed verification aborts non-zero and removes the file.
    block = src[call:src.index("$sizeKB")]
    assert "exit 1" in block and "Remove-Item" in block


def test_install_windows_ps1_verifies_in_both_download_paths():
    src = _INSTALL_WINDOWS_PS1.read_text(encoding="utf-8")
    calls = [m.start() for m in re.finditer(r"if \(-not \(Test-MeridianDownloadIntegrity -Path \$dest", src)]
    assert len(calls) == 2, "both the -Tray and the default meridian.exe download must be verified"
    tray_call, exe_call = calls
    assert '-AssetName "meridian-tray.exe"' in src[tray_call:tray_call + 200]
    assert '-AssetName "meridian.exe"' in src[exe_call:exe_call + 200]
    assert tray_call < src.index("exit 0", src.index("if ($Tray) {"))
    assert exe_call < src.index("SetEnvironmentVariable"), "PATH must not be updated before verification"
    for call in calls:
        block = src[call:call + 600]
        assert "exit 1" in block and "Remove-Item" in block


def _extract_function(src: str, name: str) -> str:
    start = src.index(f"function {name} {{")
    end = src.index("\n}\n", start) + 3
    return src[start:end]


class _Handler(http.server.SimpleHTTPRequestHandler):
    requests: list[str] = []

    def log_message(self, fmt, *args):  # keep pytest output clean
        pass

    def do_GET(self):  # noqa: N802 - stdlib naming
        type(self).requests.append(self.path)
        super().do_GET()


@pytest.fixture
def loopback_server(tmp_path):
    root = tmp_path / "www"
    root.mkdir()

    class Handler(_Handler):
        requests: list[str] = []

        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield root, f"http://127.0.0.1:{server.server_address[1]}", Handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


_PS_ASSET = "meridian-test.exe"
_PS_CONTENT = b"MZ pretend windows binary"


def _ps_case_dirs(root: Path) -> None:
    good = _sha256(_PS_CONTENT)
    layouts = {
        "ok": f"{good}  {_PS_ASSET}\n",
        "okstar": f"{good.upper()} *{_PS_ASSET}\r\n",
        "bad": f"{'0' * 64}  {_PS_ASSET}\n",
        "other": f"{good}  some-other-file.exe\n",
        "garbage": "this is not a checksum file\n",
    }
    for name, text in layouts.items():
        d = root / name
        d.mkdir()
        (d / "SHA256SUMS").write_bytes(text.encode("utf-8"))
    (root / "missing").mkdir()  # no SHA256SUMS -> 404


@pytest.mark.subprocess_isolated
@_needs_ps51
@pytest.mark.parametrize("script", [_INSTALL_PS1, _INSTALL_WINDOWS_PS1], ids=lambda p: p.name)
def test_powershell_verification_helper_end_to_end(script, loopback_server, tmp_path):
    root, base, handler = loopback_server
    _ps_case_dirs(root)
    src = script.read_text(encoding="utf-8")
    funcs = _extract_function(src, "Get-MeridianSumsEntry") + "\n" + _extract_function(
        src, "Test-MeridianDownloadIntegrity"
    )
    work = tmp_path / "ps"
    work.mkdir()
    harness = funcs + f"""
$ErrorActionPreference = 'Stop'
$results = @()
foreach ($case in @('ok','okstar','bad','other','garbage','missing')) {{
    $f = Join-Path '{work.as_posix()}' ($case + '.exe')
    [System.IO.File]::WriteAllBytes($f, [System.Text.Encoding]::ASCII.GetBytes('{_PS_CONTENT.decode()}'))
    $r = Test-MeridianDownloadIntegrity -Path $f -AssetName '{_PS_ASSET}' -SumsUrl ('{base}/' + $case + '/SHA256SUMS')
    $isBool = ($r -is [bool])
    Write-Output ('CASE ' + $case + ' result=' + $r + ' isbool=' + $isBool + ' exists=' + (Test-Path $f))
}}
# Explicit opt-out: skips verification (and the network) but is loud about it.
$env:{_OPT_OUT} = '1'
$f = Join-Path '{work.as_posix()}' 'optout.exe'
[System.IO.File]::WriteAllBytes($f, [byte[]](1,2,3))
$r = Test-MeridianDownloadIntegrity -Path $f -AssetName '{_PS_ASSET}' -SumsUrl '{base}/missing/SHA256SUMS'
Write-Output ('CASE optout result=' + $r + ' exists=' + (Test-Path $f))
"""
    hp = work / "harness.ps1"
    hp.write_bytes(b"\xef\xbb\xbf" + harness.encode("utf-8"))  # BOM: 5.1 must read it as UTF-8
    env = {k: v for k, v in os.environ.items() if k != _OPT_OUT}
    proc = subprocess.run(
        [_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(hp)],
        capture_output=True, text=True, timeout=180, env=env,
    )
    out = proc.stdout
    assert proc.returncode == 0, out + proc.stderr

    def case(name: str) -> str:
        m = re.search(rf"^CASE {name} (.*)$", out, flags=re.M)
        assert m, f"no result for {name}:\n{out}\n{proc.stderr}"
        return m.group(1).strip()

    assert case("ok") == "result=True isbool=True exists=True"
    assert case("okstar") == "result=True isbool=True exists=True"
    # Every failure mode fails closed AND deletes the download.
    for failing in ("bad", "other", "garbage", "missing"):
        assert case(failing) == "result=False isbool=True exists=False", failing
    assert "SHA-256 MISMATCH" in out
    # Opt-out: verification is skipped (file kept, result True), loudly, and the
    # checksum URL is not even requested (only the one 'missing' case hit it).
    assert case("optout") == "result=True exists=True"
    assert "SKIPPING SHA-256" in out + proc.stderr
    assert handler.requests.count("/missing/SHA256SUMS") == 1


@pytest.mark.subprocess_isolated
@_needs_ps51
@pytest.mark.parametrize("script", [_INSTALL_PS1, _INSTALL_WINDOWS_PS1], ids=lambda p: p.name)
def test_powershell_sums_parser_is_exact_about_the_asset_name(script, tmp_path):
    src = script.read_text(encoding="utf-8")
    func = _extract_function(src, "Get-MeridianSumsEntry")
    a, b = "a" * 64, "b" * 64
    harness = func + f"""
$text = "{a}  meridian.exe`n{b}  meridian.exe.sig`n{'c' * 64}  Meridian.exe`n"
Write-Output ('exact=' + (Get-MeridianSumsEntry -SumsText $text -AssetName 'meridian.exe'))
Write-Output ('prefix=' + (Get-MeridianSumsEntry -SumsText $text -AssetName 'meridian.ex'))
Write-Output ('case=' + (Get-MeridianSumsEntry -SumsText $text -AssetName 'MERIDIAN.EXE'))
Write-Output ('caseok=' + (Get-MeridianSumsEntry -SumsText $text -AssetName 'Meridian.exe'))
"""
    hp = tmp_path / "parse.ps1"
    hp.write_bytes(b"\xef\xbb\xbf" + harness.encode("utf-8"))
    proc = subprocess.run(
        [_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(hp)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"exact={a}" in proc.stdout
    assert "prefix=" in proc.stdout and f"prefix={a}" not in proc.stdout
    assert re.search(r"^case=\s*$", proc.stdout, flags=re.M), proc.stdout
    assert f"caseok={'c' * 64}" in proc.stdout


@pytest.mark.parametrize("script", [_INSTALL_PS1, _INSTALL_WINDOWS_PS1], ids=lambda p: p.name)
def test_powershell_installers_stay_ascii_and_ps51_safe(script):
    raw = script.read_bytes()
    assert not [b for b in raw if b > 0x7F], f"{script.name} must stay pure ASCII (PowerShell 5.1 reads BOM-less UTF-8 as cp1252)"
    # Same PS7-only operator check as tests/test_install_script_hygiene.py (which
    # only globs scripts/, so the root install.ps1 is covered here).
    code = re.sub(r"<#.*?#>", "", raw.decode("ascii"), flags=re.S)
    code = "\n".join(ln.split("#", 1)[0] for ln in code.splitlines())
    bad = [ln.strip() for ln in code.splitlines() if re.search(r"(?:[\)\]\w\}]\?\.|\?\?)", ln)]
    assert not bad, f"PowerShell 7-only operators in {script.name}: {bad}"


@pytest.mark.subprocess_isolated
@_needs_ps51
@pytest.mark.parametrize("script", [_INSTALL_PS1, _INSTALL_WINDOWS_PS1], ids=lambda p: p.name)
def test_powershell_installers_parse_on_windows_powershell_51(script):
    ps_script = (
        "$tokens=$null;$errors=$null;"
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{script.as_posix()}',[ref]$tokens,[ref]$errors)|Out-Null;"
        "if($errors){$errors|ForEach-Object{Write-Output ($_.Extent.StartLineNumber.ToString()+': '+$_.Message)};exit 1}"
        "else{Write-Output 'PARSE_OK';exit 0}"
    )
    proc = subprocess.run(
        [_POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", ps_script],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0 and "PARSE_OK" in proc.stdout, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# meridian.tunnel_client: pinned, hash-verified codebase-memory-mcp download
# ---------------------------------------------------------------------------

_CBM_BIN = b"\x7fELF-not-really" + b"\x00" * (1024 * 1024 + 16)  # > 1 MB sanity floor


def _tar_gz(members: dict[str, bytes], *, extra=None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        if extra:
            extra(tf)
    return buf.getvalue()


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


class _FakeHttpx:
    """Stand-in for httpx.AsyncClient serving one body; records requested URLs."""

    def __init__(self, monkeypatch, body: bytes | Exception):
        import httpx

        self.urls: list[str] = []
        outer = self

        class Resp:
            def raise_for_status(self):
                pass

            @property
            def content(self):
                return body

        class Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, **kw):
                outer.urls.append(url)
                if isinstance(body, Exception):
                    raise body
                return Resp()

        monkeypatch.setattr(httpx, "AsyncClient", Client)


def _pin_platform(monkeypatch, tmp_path, *, plat: str, machine: str, pin: dict) -> Path:
    import platform as _platform

    bindir = tmp_path / "managed-bin"
    monkeypatch.setattr(tc, "_managed_bin_dir", lambda: bindir)
    monkeypatch.setattr(tc.sys, "platform", plat)
    monkeypatch.setattr(_platform, "machine", lambda: machine)
    monkeypatch.setattr(tc, "_CBM_PINNED_ASSETS", pin)
    return bindir


_LINUX_CBM_ASSET = "codebase-memory-mcp-linux-amd64-portable.tar.gz"


def test_real_pin_table_is_well_formed():
    assert re.fullmatch(r"v\d+\.\d+\.\d+", tc._CBM_PINNED_TAG)
    assert set(tc._CBM_PINNED_ASSETS) == {
        ("windows", "amd64"), ("windows", "arm64"),
        ("darwin", "amd64"), ("darwin", "arm64"),
        ("linux", "amd64"), ("linux", "arm64"),
    }
    seen_hashes = set()
    for (os_key, arch_key), (name, digest) in tc._CBM_PINNED_ASSETS.items():
        assert re.fullmatch(r"[0-9a-f]{64}", digest), (os_key, arch_key)
        assert digest not in seen_hashes, "two platforms must not share a pin"
        seen_hashes.add(digest)
        assert name.startswith("codebase-memory-mcp-") and f"-{os_key}-{arch_key}" in name
        assert name.endswith(".zip" if os_key == "windows" else ".tar.gz")
        assert "/" not in name and "\\" not in name


@pytest.mark.parametrize(
    "plat,machine,expected",
    [
        ("win32", "AMD64", ("windows", "amd64")),
        ("win32", "ARM64", ("windows", "arm64")),
        ("darwin", "arm64", ("darwin", "arm64")),
        ("darwin", "x86_64", ("darwin", "amd64")),
        ("linux", "x86_64", ("linux", "amd64")),
        ("linux", "aarch64", ("linux", "arm64")),
        ("freebsd14", "amd64", None),
        ("linux", "riscv64", None),
        ("linux", "i686", None),
    ],
)
def test_platform_key_mapping(monkeypatch, plat, machine, expected):
    import platform as _platform

    monkeypatch.setattr(tc.sys, "platform", plat)
    monkeypatch.setattr(_platform, "machine", lambda: machine)
    assert tc._cbm_platform_key() == expected


def test_download_uses_the_pinned_tag_url_and_never_the_latest_api(monkeypatch, tmp_path):
    archive = _tar_gz({"codebase-memory-mcp": _CBM_BIN})
    bindir = _pin_platform(
        monkeypatch, tmp_path, plat="linux", machine="x86_64",
        pin={("linux", "amd64"): (_LINUX_CBM_ASSET, _sha256(archive))},
    )
    http = _FakeHttpx(monkeypatch, archive)
    result = asyncio.run(tc._download_codebase_memory_mcp())
    assert result == str(bindir / "codebase-memory-mcp")
    assert http.urls == [
        f"https://github.com/DeusData/codebase-memory-mcp/releases/download/{tc._CBM_PINNED_TAG}/{_LINUX_CBM_ASSET}"
    ]
    import inspect

    source = inspect.getsource(tc._download_codebase_memory_mcp)
    assert "releases/latest" not in source, "the unpinned latest-release lookup must stay gone"


def test_hash_mismatch_refuses_to_install_and_never_extracts(monkeypatch, tmp_path, capsys):
    archive = _tar_gz({"codebase-memory-mcp": _CBM_BIN})
    bindir = _pin_platform(
        monkeypatch, tmp_path, plat="linux", machine="x86_64",
        pin={("linux", "amd64"): (_LINUX_CBM_ASSET, "0" * 64)},  # pin != served bytes
    )
    _FakeHttpx(monkeypatch, archive)
    extracted = []
    monkeypatch.setattr(tc, "_extract_cbm_binary", lambda *a, **kw: extracted.append(a) or b"x")
    assert asyncio.run(tc._download_codebase_memory_mcp()) is None
    assert extracted == [], "the archive must not even be opened before its hash matches"
    err = capsys.readouterr().err
    assert "MISMATCH" in err and _sha256(archive) in err
    assert not (bindir / "codebase-memory-mcp").exists()
    assert not bindir.exists() or list(bindir.iterdir()) == [], "nothing may be left on disk"


def test_hash_mismatch_with_a_swapped_archive_of_the_same_shape(monkeypatch, tmp_path):
    """A malicious archive laid out exactly like the real one still fails the pin."""
    genuine = _tar_gz({"codebase-memory-mcp": _CBM_BIN})
    evil = _tar_gz({"codebase-memory-mcp": b"EVIL" + _CBM_BIN})
    bindir = _pin_platform(
        monkeypatch, tmp_path, plat="linux", machine="x86_64",
        pin={("linux", "amd64"): (_LINUX_CBM_ASSET, _sha256(genuine))},
    )
    _FakeHttpx(monkeypatch, evil)
    assert asyncio.run(tc._download_codebase_memory_mcp()) is None
    assert not (bindir / "codebase-memory-mcp").exists()


def test_verified_tar_gz_installs_only_the_executable_and_marks_it_executable(monkeypatch, tmp_path):
    archive = _tar_gz({
        "codebase-memory-mcp-linux-amd64/install.sh": b"#!/bin/sh\necho installer\n",
        "codebase-memory-mcp-linux-amd64/README.md": b"docs",
        "codebase-memory-mcp-linux-amd64/codebase-memory-mcp": _CBM_BIN,
    })
    bindir = _pin_platform(
        monkeypatch, tmp_path, plat="linux", machine="x86_64",
        pin={("linux", "amd64"): (_LINUX_CBM_ASSET, _sha256(archive))},
    )
    _FakeHttpx(monkeypatch, archive)
    result = asyncio.run(tc._download_codebase_memory_mcp())
    assert result == str(bindir / "codebase-memory-mcp")
    assert (bindir / "codebase-memory-mcp").read_bytes() == _CBM_BIN
    assert sorted(p.name for p in bindir.iterdir()) == ["codebase-memory-mcp"], "install.sh etc. must not be extracted"
    if os.name != "nt":
        assert os.access(result, os.X_OK)


def test_verified_zip_installs_the_windows_executable(monkeypatch, tmp_path):
    archive = _zip({"install.ps1": b"Write-Host hi", "codebase-memory-mcp.exe": _CBM_BIN})
    bindir = _pin_platform(
        monkeypatch, tmp_path, plat="win32", machine="AMD64",
        pin={("windows", "amd64"): ("codebase-memory-mcp-windows-amd64.zip", _sha256(archive))},
    )
    _FakeHttpx(monkeypatch, archive)
    result = asyncio.run(tc._download_codebase_memory_mcp())
    assert result == str(bindir / "codebase-memory-mcp.exe")
    assert (bindir / "codebase-memory-mcp.exe").read_bytes() == _CBM_BIN
    assert sorted(p.name for p in bindir.iterdir()) == ["codebase-memory-mcp.exe"]


def test_member_names_never_choose_where_files_are_written(monkeypatch, tmp_path):
    archive = _tar_gz({"../../escape/codebase-memory-mcp": _CBM_BIN})
    bindir = _pin_platform(
        monkeypatch, tmp_path, plat="linux", machine="x86_64",
        pin={("linux", "amd64"): (_LINUX_CBM_ASSET, _sha256(archive))},
    )
    _FakeHttpx(monkeypatch, archive)
    assert asyncio.run(tc._download_codebase_memory_mcp()) == str(bindir / "codebase-memory-mcp")
    assert not (tmp_path.parent / "escape").exists()
    assert not (tmp_path / "escape").exists()
    assert sorted(p.name for p in bindir.iterdir()) == ["codebase-memory-mcp"]


def test_verified_archive_without_the_executable_installs_nothing(monkeypatch, tmp_path, capsys):
    archive = _tar_gz({"install.sh": b"#!/bin/sh\n", "README.md": b"docs"})
    bindir = _pin_platform(
        monkeypatch, tmp_path, plat="linux", machine="x86_64",
        pin={("linux", "amd64"): (_LINUX_CBM_ASSET, _sha256(archive))},
    )
    _FakeHttpx(monkeypatch, archive)
    assert asyncio.run(tc._download_codebase_memory_mcp()) is None
    assert "does not contain" in capsys.readouterr().err
    assert not bindir.exists() or list(bindir.iterdir()) == []


def test_symlink_member_is_not_treated_as_the_executable():
    def add_link(tf):
        link = tarfile.TarInfo("codebase-memory-mcp")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)

    archive = _tar_gz({}, extra=add_link)
    assert tc._extract_cbm_binary(archive, _LINUX_CBM_ASSET, "codebase-memory-mcp") is None


def test_executable_larger_than_the_cap_is_refused(monkeypatch):
    monkeypatch.setattr(tc, "_CBM_MAX_BINARY_BYTES", 64)
    tar_archive = _tar_gz({"codebase-memory-mcp": b"x" * 200})
    assert tc._extract_cbm_binary(tar_archive, "a.tar.gz", "codebase-memory-mcp") is None
    zip_archive = _zip({"codebase-memory-mcp.exe": b"x" * 200})
    assert tc._extract_cbm_binary(zip_archive, "a.zip", "codebase-memory-mcp.exe") is None
    ok = _tar_gz({"codebase-memory-mcp": b"x" * 64})
    assert tc._extract_cbm_binary(ok, "a.tar.gz", "codebase-memory-mcp") == b"x" * 64


def test_unknown_archive_type_is_refused():
    assert tc._extract_cbm_binary(b"whatever", "codebase-memory-mcp-linux-amd64", "codebase-memory-mcp") is None


def test_tiny_executable_is_rejected_by_the_size_sanity_check(monkeypatch, tmp_path, capsys):
    archive = _tar_gz({"codebase-memory-mcp": b"tiny"})
    bindir = _pin_platform(
        monkeypatch, tmp_path, plat="linux", machine="x86_64",
        pin={("linux", "amd64"): (_LINUX_CBM_ASSET, _sha256(archive))},
    )
    _FakeHttpx(monkeypatch, archive)
    assert asyncio.run(tc._download_codebase_memory_mcp()) is None
    assert "too small" in capsys.readouterr().err
    assert not bindir.exists() or list(bindir.iterdir()) == []


def test_unsupported_platform_fails_closed_without_network(monkeypatch, tmp_path, capsys):
    import platform as _platform

    monkeypatch.setattr(tc, "_managed_bin_dir", lambda: tmp_path / "bin")
    monkeypatch.setattr(tc.sys, "platform", "freebsd14")
    monkeypatch.setattr(_platform, "machine", lambda: "amd64")
    http = _FakeHttpx(monkeypatch, RuntimeError("network must not be touched"))
    assert asyncio.run(tc._download_codebase_memory_mcp()) is None
    assert http.urls == []
    assert "refusing to download an unverified binary" in capsys.readouterr().err


def test_no_unpinned_fallback_exists_for_a_platform_missing_from_the_table(monkeypatch, tmp_path):
    bindir = _pin_platform(monkeypatch, tmp_path, plat="linux", machine="x86_64", pin={})
    http = _FakeHttpx(monkeypatch, RuntimeError("network must not be touched"))
    assert asyncio.run(tc._download_codebase_memory_mcp()) is None
    assert http.urls == []
    assert not bindir.exists()


def test_download_error_leaves_nothing_behind(monkeypatch, tmp_path):
    archive = _tar_gz({"codebase-memory-mcp": _CBM_BIN})
    bindir = _pin_platform(
        monkeypatch, tmp_path, plat="linux", machine="x86_64",
        pin={("linux", "amd64"): (_LINUX_CBM_ASSET, _sha256(archive))},
    )
    _FakeHttpx(monkeypatch, RuntimeError("connection reset"))
    assert asyncio.run(tc._download_codebase_memory_mcp()) is None
    assert not bindir.exists() or list(bindir.iterdir()) == []


def test_failed_atomic_replace_cleans_up_the_partial_file(monkeypatch, tmp_path):
    archive = _tar_gz({"codebase-memory-mcp": _CBM_BIN})
    bindir = _pin_platform(
        monkeypatch, tmp_path, plat="linux", machine="x86_64",
        pin={("linux", "amd64"): (_LINUX_CBM_ASSET, _sha256(archive))},
    )
    _FakeHttpx(monkeypatch, archive)

    def boom(*a, **kw):
        raise PermissionError("in use")

    with monkeypatch.context() as scoped:
        scoped.setattr(tc.os, "replace", boom)
        assert asyncio.run(tc._download_codebase_memory_mcp()) is None
    assert list(bindir.iterdir()) == [], "the .part file must not be left in the managed bin dir"


def test_sha256_comparison_is_case_insensitive_and_exact():
    data = b"abc"
    good = _sha256(data)
    assert tc._cbm_sha256_matches(data, good)
    assert tc._cbm_sha256_matches(data, good.upper())
    assert tc._cbm_sha256_matches(data, f"  {good}\n")
    assert not tc._cbm_sha256_matches(data, good[:-1] + ("0" if good[-1] != "0" else "1"))
    assert not tc._cbm_sha256_matches(data, "")
    assert not tc._cbm_sha256_matches(b"abd", good)


def test_ensure_still_prefers_an_already_installed_binary(monkeypatch):
    monkeypatch.setattr(tc, "_find_codebase_memory_mcp", lambda: "/usr/bin/codebase-memory-mcp")
    called = []

    async def fake_download():
        called.append(True)
        return "/nope"

    monkeypatch.setattr(tc, "_download_codebase_memory_mcp", fake_download)
    assert asyncio.run(tc._ensure_codebase_memory_mcp()) == "/usr/bin/codebase-memory-mcp"
    assert called == []


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
