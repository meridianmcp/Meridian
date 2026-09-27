"""Install-script hygiene: the scripts users download and run must name the right
npm package and must run on the Windows PowerShell 5.1 that ships with Windows.

Found by the 2026-09-27 install/tunnel audit (workflow wf_5ef779aa-99f):

- scripts/install_tunnel.ps1 / install_tunnel.sh told users to run
  ``npm i -g meridian-mcp``. That unscoped name is an unrelated third-party
  package ("Meteora DLMM LP agent", different maintainer). Meridian publishes
  ``@meridianmcp/mcp`` (npm/package.json), whose *bin* happens to be named
  ``meridian-mcp`` -- which is how the wrong name slipped in. Installing the
  wrong package is a supply-chain risk, so no tracked file may suggest it.
- install_tunnel.ps1 and install_watcher.ps1 used the ``?.`` null-conditional
  operator, which only exists in PowerShell 7. Windows PowerShell 5.1 refused
  to parse either file, so both installers failed on a stock Windows machine.
  install_watcher.ps1 also carried BOM-less non-ASCII bytes, which 5.1 reads as
  cp1252 (see also test_14575683_jq_fastpath_hitl's ASCII check).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO / "scripts"
_INSTALL_PS1 = sorted(_SCRIPTS.glob("install*.ps1"))

# npm install / npx invocations of the UNSCOPED package. The scoped
# ``@meridianmcp/mcp`` never matches: the name must follow the verb directly.
_UNSCOPED_NPM_HINT = re.compile(
    r"\b(?:npm\s+(?:i|install)(?:\s+(?:-g|--global))?|npx(?:\s+-y)?)\s+meridian-mcp\b"
)

# PowerShell 7-only operators: null-conditional member access (``)?.`` /
# ``$x?.``) and null-coalescing (``??`` / ``??=``). Checked on code only, with
# comments stripped, so prose mentioning them does not trip the check.
_PS7_ONLY = re.compile(r"(?:[\)\]\w\}]\?\.|\?\?)")

_TEXT_SUFFIXES = {".ps1", ".sh", ".md", ".py", ".ts", ".js", ".cjs", ".html", ".json", ".toml", ".yml", ".yaml", ".txt"}
_SKIP_PARTS = {"node_modules", ".git", "dist", "worktrees", ".venv", "__pycache__"}


def _tracked_text_files() -> list[Path]:
    git = shutil.which("git")
    if git is None or not (_REPO / ".git").exists():
        pytest.skip("not a git checkout; cannot enumerate tracked files")
    out = subprocess.run(
        [git, "-C", str(_REPO), "ls-files", "-z"],
        capture_output=True, check=True, timeout=60,
    ).stdout.decode("utf-8", "replace")
    files = []
    for rel in filter(None, out.split("\0")):
        p = _REPO / rel
        # Filter on the REPO-RELATIVE parts: the checkout itself may live under
        # a directory named e.g. "worktrees", which must not exclude everything.
        if p.suffix.lower() in _TEXT_SUFFIXES and not (_SKIP_PARTS & set(Path(rel).parts)):
            files.append(p)
    assert files, "no tracked text files found; the file filter is wrong"
    return files


def _strip_ps_comments(text: str) -> str:
    text = re.sub(r"<#.*?#>", "", text, flags=re.S)
    return "\n".join(line.split("#", 1)[0] if "#" in line else line for line in text.splitlines())


def test_install_scripts_exist():
    names = {p.name for p in _INSTALL_PS1}
    assert {"install_tunnel.ps1", "install_watcher.ps1"} <= names


def test_no_tracked_file_suggests_the_unscoped_meridian_mcp_npm_package():
    this_file = Path(__file__).resolve()
    hits = []
    for p in _tracked_text_files():
        if p.resolve() == this_file:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _UNSCOPED_NPM_HINT.search(line):
                hits.append(f"{p.relative_to(_REPO).as_posix()}:{lineno}: {line.strip()[:120]}")
    assert not hits, (
        "these lines tell users to install the unrelated third-party npm package "
        "'meridian-mcp'; Meridian's package is '@meridianmcp/mcp':\n" + "\n".join(hits)
    )


def test_tunnel_install_hints_name_the_scoped_package():
    for name in ("install_tunnel.ps1", "install_tunnel.sh"):
        text = (_SCRIPTS / name).read_text(encoding="utf-8")
        assert "npm i -g @meridianmcp/mcp" in text, name


@pytest.mark.parametrize("script", _INSTALL_PS1, ids=lambda p: p.name)
def test_install_ps1_is_ascii_or_has_bom(script: Path):
    raw = script.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return
    non_ascii = [i for i, b in enumerate(raw) if b > 0x7F]
    assert not non_ascii, (
        f"{script.name} is BOM-less UTF-8 with non-ASCII bytes at offsets {non_ascii[:10]}; "
        "Windows PowerShell 5.1 reads that as cp1252"
    )


@pytest.mark.parametrize("script", _INSTALL_PS1, ids=lambda p: p.name)
def test_install_ps1_uses_no_powershell7_only_operators(script: Path):
    code = _strip_ps_comments(script.read_text(encoding="utf-8", errors="replace"))
    bad = [
        f"{lineno}: {line.strip()[:120]}"
        for lineno, line in enumerate(code.splitlines(), 1)
        if _PS7_ONLY.search(line)
    ]
    assert not bad, f"{script.name} uses PowerShell 7-only operators (?. / ??):\n" + "\n".join(bad)


def _windows_powershell_51() -> str | None:
    # Deliberately NOT pwsh: PowerShell 7 accepts ?. and would hide the bug.
    return shutil.which("powershell")


@pytest.mark.subprocess_isolated
@pytest.mark.skipif(_windows_powershell_51() is None, reason="Windows PowerShell 5.1 not available")
@pytest.mark.parametrize("script", _INSTALL_PS1, ids=lambda p: p.name)
def test_install_ps1_parses_on_windows_powershell_51(script: Path):
    ps_script = (
        "$tokens=$null;$errors=$null;"
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{script.as_posix()}',[ref]$tokens,[ref]$errors)|Out-Null;"
        "if($errors){$errors|ForEach-Object{Write-Output ($_.Extent.StartLineNumber.ToString()+': '+$_.Message)};exit 1}"
        "else{Write-Output 'PARSE_OK';exit 0}"
    )
    proc = subprocess.run(
        [_windows_powershell_51(), "-NoProfile", "-NonInteractive", "-Command", ps_script],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0 and "PARSE_OK" in proc.stdout, (
        f"{script.name} does not parse on Windows PowerShell 5.1:\n{proc.stdout}\n{proc.stderr}"
    )
