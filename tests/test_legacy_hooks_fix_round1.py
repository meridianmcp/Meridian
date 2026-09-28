"""55d48d69 fix round 1 -- the legacy hooks the launcher fix (9a4442a1) made blocking.

Before 9a4442a1 every registered PowerShell hook failed to start, so none of these
hooks ever blocked anything. Once their ``exit 2`` became real, the verification
run found each of them blocking ordinary work. This module pins the fixes, running
the REAL scripts (``powershell -File`` for the .ps1, ``bash`` for the .sh twin)
against the exact payloads from the verification findings:

* secret_guard -- source/test/template files and ordinary shell idioms are not
  secrets; real credential files and real environment dumps still are
  (meridian.toml now included, Grep's ``glob`` field now checked).
* worktree_guard -- a worktree session may write the temp dir / scratchpad and
  ~/.claude/plans; the same-file lock is per working tree and warn-only.
* dependency_install_guard -- quotes, redirections, relative directory installs,
  wrapped invocations (pixi run, full-path python -m pip, uv pip, pipes, newlines).
* sprint_guard -- only a session that claimed a sprint item is held back, the
  destructive worktree-sweep trigger is gone, a down server fails open fast.
* hitl_guard -- names the plain-text fallback; pkg_install_guard /
  test_tamper_guard -- no multi-second wait on a down server.
* every one of them honours the owner kill switch (MERIDIAN_GUARD=off|advisory,
  guard.off / guard.advisory, MERIDIAN_GUARD_DISABLE=<hook name>).

Temp git repositories only -- nothing here writes into this checkout's .git.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / ".claude" / "hooks"

pytestmark = [pytest.mark.subprocess_isolated, pytest.mark.timeout(600)]


def _powershell() -> str | None:
    if os.name != "nt":
        return None
    return shutil.which("powershell") or shutil.which("powershell.exe")


def _bash() -> str | None:
    for c in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
        if os.name == "nt" and Path(c).exists():
            return c
    return shutil.which("bash")


POWERSHELL = _powershell()
BASH = _bash()
SHELLS = [pytest.param("ps1", marks=pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell only")),
          pytest.param("sh", marks=pytest.mark.skipif(BASH is None, reason="bash unavailable"))]

_CRASH = frozenset({0xC0000005, 0xC000007B, 0xC0000135, 0xC0000142, 0xC000013A, 3221225773})


def _clean_env(tmp_path: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    keep = ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATH", "PATHEXT", "COMSPEC", "PROCESSOR_ARCHITECTURE",
            "NUMBER_OF_PROCESSORS", "MSYSTEM")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    home = tmp_path / "home"
    lad = tmp_path / "lad"
    tmp = tmp_path / "tmp"
    for d in (home, lad, tmp):
        d.mkdir(exist_ok=True)
    env.update({"USERPROFILE": str(home), "HOME": str(home), "LOCALAPPDATA": str(lad),
                "TEMP": str(tmp), "TMP": str(tmp), "MERIDIAN_URL": _free_url()})
    env.update(extra or {})
    return env


def _free_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}"


def _run(shell: str, hook: str, payload: Any, env: dict[str, str], cwd: Path | None = None,
         timeout: float = 90) -> subprocess.CompletedProcess:
    data = payload if isinstance(payload, str) else json.dumps(payload)
    if shell == "ps1":
        argv = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(HOOKS / f"{hook}.ps1")]
    else:
        argv = [BASH, str(HOOKS / f"{hook}.sh")]
    last = None
    for _ in range(3):
        try:
            last = subprocess.run(argv, input=data.encode("utf-8"), capture_output=True, env=env,
                                  cwd=str(cwd or REPO), timeout=timeout)
        except subprocess.TimeoutExpired:
            continue
        if (last.returncode & 0xFFFFFFFF) in _CRASH:
            continue
        return last
    if last is None:
        pytest.skip("the shell never completed (host contention)")
    return last


def _ctx(r: subprocess.CompletedProcess) -> str | None:
    out = r.stdout.decode("utf-8", "replace").strip()
    if not out:
        return None
    return json.loads(out)["hookSpecificOutput"].get("additionalContext")


def _pre(tool: str, ti: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return dict({"session_id": "fix-round-1", "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": ti}, **extra)


# ---------------------------------------------------------------------------
# secret_guard
# ---------------------------------------------------------------------------

_SECRET_ALLOW = [
    _pre("Read", {"file_path": str(REPO / "meridian" / "secret_redaction.py")}),
    _pre("Read", {"file_path": str(REPO / "tests" / "test_secret_redaction.py")}),
    _pre("Read", {"file_path": str(REPO / ".claude" / "hooks" / "secret_guard.ps1")}),
    _pre("Read", {"file_path": str(REPO / "tests" / "test_dd07ece0_handoff_token.py")}),
    _pre("Read", {"file_path": str(REPO / "scripts" / "refresh_token.py")}),
    _pre("Read", {"file_path": str(REPO / "secrets.env.example")}),
    _pre("Read", {"file_path": str(REPO / ".env.example")}),
    _pre("Grep", {"pattern": "_SENSITIVE_BASENAME_PATTERNS", "path": str(REPO / "meridian" / "secret_redaction.py")}),
    _pre("Grep", {"pattern": "project_id", "path": str(REPO / "meridian.toml")}),
    _pre("Bash", {"command": "set -euo pipefail; pixi run test -n 3 tests/test_guard_core.py"}),
    _pre("Bash", {"command": "export PYTHONPATH=$PWD && python -m pytest tests/test_guard_core.py -q"}),
    _pre("Bash", {"command": "env PYTHONUTF8=1 python -m meridian.cbm_registry --resolve ."}),
    _pre("Bash", {"command": "cat server.json | jq '.key'"}),
    _pre("Bash", {"command": "printenv PATH | tr ':' '\\n' | head"}),
    _pre("Bash", {"command": "cat secrets.env.example"}),
    _pre("Bash", {"command": "cat meridian/config.py | grep -n os.environ"}),
    _pre("Bash", {"command": "cat meridian/static/dashboard.ts | grep import.meta.env"}),
    _pre("Bash", {"command": "git log --oneline --grep printenv"}),
    _pre("Bash", {"command": "env MERIDIAN_GUARD=off pixi run test"}),
    _pre("PowerShell", {"command": "Get-Content .env.example"}),
    _pre("PowerShell", {"command": "Set-Location meridian; Get-ChildItem"}),
]
_SECRET_BLOCK = [
    _pre("Read", {"file_path": "C:/proj/.env"}),
    _pre("Read", {"file_path": "/home/u/.ssh/id_rsa"}),
    _pre("Read", {"file_path": "certs/server.pem"}),
    _pre("Read", {"file_path": str(REPO / "meridian.toml")}),
    _pre("Grep", {"pattern": "KEY", "path": ".env"}),
    _pre("Grep", {"pattern": "KEY", "path": str(REPO), "glob": ".env"}),
    _pre("Grep", {"pattern": "password", "path": str(REPO / "meridian.toml")}),
    _pre("Bash", {"command": "cat .env"}),
    _pre("Bash", {"command": "printenv"}),
    _pre("Bash", {"command": "env"}),
    _pre("Bash", {"command": "set"}),
    _pre("Bash", {"command": "export -p"}),
    _pre("Bash", {"command": "env | sort"}),
    _pre("Bash", {"command": "head -50 .env"}),
    _pre("Bash", {"command": "grep = .env"}),
    _pre("Bash", {"command": "python -c \"print(open('.env').read())\""}),
    _pre("Bash", {"command": "cat meridian.toml"}),
    _pre("Bash", {"command": "cat ~/.ssh/id_rsa"}),
    _pre("PowerShell", {"command": "Get-Content .env"}),
    _pre("PowerShell", {"command": "ls env:"}),
    _pre("PowerShell", {"command": "[Environment]::GetEnvironmentVariables()"}),
    _pre("PowerShell", {"command": "[IO.File]::ReadAllText('.env')"}),
    _pre("PowerShell", {"command": "Select-String -Path .env -Pattern KEY"}),
]


@pytest.mark.parametrize("shell", SHELLS)
def test_secret_guard_allows_source_tests_templates_and_shell_idioms(shell, tmp_path):
    env = _clean_env(tmp_path)
    bad = []
    for p in _SECRET_ALLOW:
        r = _run(shell, "secret_guard", p, env)
        if r.returncode != 0:
            bad.append((p["tool_input"], r.returncode, r.stderr[-300:]))
    assert bad == []


@pytest.mark.parametrize("shell", SHELLS)
def test_secret_guard_still_blocks_real_credential_reads_and_dumps(shell, tmp_path):
    env = _clean_env(tmp_path)
    missed = []
    for p in _SECRET_BLOCK:
        r = _run(shell, "secret_guard", p, env)
        if r.returncode != 2 or b"14491654" not in r.stderr:
            missed.append((p["tool_input"], r.returncode))
    assert missed == []


def test_secret_redaction_module_mirrors_the_hook():
    from meridian.secret_redaction import is_sensitive_path

    for ok in ("secret_redaction.py", "test_secret_redaction.py", "refresh_token.py", "secret_guard.ps1",
               ".env.example", "secrets.env.example", "test_dd07ece0_handoff_token.py", "C:\\x\\secret_guard.sh"):
        assert not is_sensitive_path(ok), ok
    for bad in (".env", "meridian.toml", "secrets.yaml", "refresh_token.json", "id_rsa", "my_password_file.txt"):
        assert is_sensitive_path(bad), bad


# ---------------------------------------------------------------------------
# owner kill switch (every legacy blocking hook)
# ---------------------------------------------------------------------------

_KILL_CASES = [
    ("secret_guard", _pre("Read", {"file_path": "C:/proj/.env"})),
    ("hitl_guard", _pre("AskUserQuestion", {"questions": []})),
    ("dependency_install_guard", _pre("Bash", {"command": "pip install totally-unheard-of-pkg-9f2b31a4"})),
]


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("hook,payload", _KILL_CASES, ids=[c[0] for c in _KILL_CASES])
def test_kill_switch_covers_legacy_hooks(shell, hook, payload, stub, tmp_path):
    # hitl_guard's enforce-mode block (fix round 2) is now conditional on Meridian
    # being reachable, so give every case a reachable stub -- harmless for the other
    # hooks here, which never look at MERIDIAN_URL.
    base = _clean_env(tmp_path, {"MERIDIAN_URL": stub})
    assert _run(shell, hook, payload, base).returncode == 2, "precondition: the call is blocked in enforce mode"
    for extra in ({"MERIDIAN_GUARD": "off"}, {"MERIDIAN_GUARD_DISABLE": f"G1, {hook}"}):
        r = _run(shell, hook, payload, dict(base, **extra))
        assert r.returncode == 0 and not r.stdout.strip(), (extra, r.stderr[-300:])
    r = _run(shell, hook, payload, dict(base, MERIDIAN_GUARD="advisory"))
    assert r.returncode == 0 and "advisory, not blocked" in (_ctx(r) or ""), r.stdout[-300:]
    gdir = Path(base["LOCALAPPDATA"]) / "meridian" / "guard"
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "guard.off").write_text("", encoding="utf-8")
    try:
        assert _run(shell, hook, payload, base).returncode == 0, "guard.off sentinel"
    finally:
        (gdir / "guard.off").unlink()
    (gdir / "guard.advisory").write_text("", encoding="utf-8")
    try:
        r = _run(shell, hook, payload, base)
        assert r.returncode == 0 and _ctx(r), "guard.advisory sentinel"
    finally:
        (gdir / "guard.advisory").unlink()


@pytest.mark.parametrize("shell", SHELLS)
def test_hitl_guard_names_the_plain_text_fallback(shell, stub, tmp_path):
    # Original intended behaviour, preserved by fix round 2: when Meridian IS
    # reachable it still blocks the native ask and redirects to request_hitl.
    r = _run(shell, "hitl_guard", _pre("AskUserQuestion", {"questions": [{"question": "Which?", "options": []}]}),
             _clean_env(tmp_path, {"MERIDIAN_URL": stub}))
    assert r.returncode == 2
    assert b"request_hitl" in r.stderr and b"plain text" in r.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_hitl_guard_fails_open_when_meridian_unreachable(shell, tmp_path):
    # fix round 2 (bug 2): with NO reachable Meridian (MERIDIAN_URL points at a free
    # port with no listener, same as _clean_env's default), the native AskUserQuestion
    # must be allowed through rather than leaving the session with no way to ask.
    env = _clean_env(tmp_path)
    t = time.monotonic()
    r = _run(shell, "hitl_guard", _pre("AskUserQuestion", {"questions": [{"question": "Which?", "options": []}]}),
             env)
    took = time.monotonic() - t
    assert r.returncode == 0, r.stderr[-300:]
    assert b"b8fbb4cb" in r.stderr and b"fail-open" in r.stderr
    assert not r.stdout.strip(), "no additionalContext -- this is a plain allow, not advisory mode"
    assert took < 15, took


@pytest.mark.parametrize("shell", SHELLS)
def test_hitl_guard_denies_when_meridian_reachable_but_erroring(shell, tmp_path):
    # A reachable TCP port that answers non-2xx (or nothing sensible) for /health must
    # be treated the same as unreachable -- fail open, per the bug report's "timeout,
    # connection error, non-2xx" wording.
    class _Err(BaseHTTPRequestHandler):
        def log_message(self, *_a: Any) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            self.send_response(503)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Err)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        env = _clean_env(tmp_path, {"MERIDIAN_URL": url})
        r = _run(shell, "hitl_guard", _pre("AskUserQuestion", {"questions": [{"question": "Which?", "options": []}]}),
                 env)
        assert r.returncode == 0, r.stderr[-300:]
        assert b"fail-open" in r.stderr
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
# dependency_install_guard
# ---------------------------------------------------------------------------

_UNKNOWN = "totally-unheard-of-pkg-9f2b31a4"
_DEP_ALLOW = [
    ("Bash", "python -m pip install -e extensions/meridian-docs"),
    ("PowerShell", "python -m pip install -e extensions\\meridian-docs"),
    ("Bash", 'pip install -e ".[dev]"'),
    ("Bash", 'git commit -m "docs: guard; pip install now verified"'),
    ("Bash", "echo 'step 2; npm install left-pad later'"),
    ("Bash", "pip install fastapi 2>&1 | tail -3"),
    ("Bash", "pip install fastapi > /tmp/pip.log"),
    ("Bash", "pixi run test"),
    ("Bash", "npm ci"),
]
_DEP_BLOCK = [
    ("Bash", f"pixi run pip install {_UNKNOWN}"),
    ("Bash", f"C:/Python312/python.exe -m pip install {_UNKNOWN}"),
    ("Bash", f"uv pip install {_UNKNOWN}"),
    ("Bash", f"echo start\npip install {_UNKNOWN}"),
    ("Bash", f"ls | pip install {_UNKNOWN}"),
    ("Bash", f"FOO=1 sudo pip install {_UNKNOWN}"),
    ("Bash", "npm install someuser/some-repo-xyz-9f2b31a4"),
    ("Bash", "pip install git+https://github.com/example/totally-unheard-of-pkg.git"),
    ("PowerShell", f"pip install {_UNKNOWN}"),
]


@pytest.mark.parametrize("shell", SHELLS)
def test_dependency_guard_reads_commands_like_a_shell(shell, tmp_path):
    env = _clean_env(tmp_path)
    wrong = []
    for tool, cmd in _DEP_ALLOW:
        r = _run(shell, "dependency_install_guard", _pre(tool, {"command": cmd}, cwd=str(REPO)), env)
        if r.returncode != 0:
            wrong.append(("should allow", cmd, r.stderr[-200:]))
    for tool, cmd in _DEP_BLOCK:
        r = _run(shell, "dependency_install_guard", _pre(tool, {"command": cmd}, cwd=str(REPO)), env)
        if r.returncode != 2:
            wrong.append(("should block", cmd, r.returncode))
    assert wrong == []


# ---------------------------------------------------------------------------
# worktree_guard (temp git repo + linked worktrees)
# ---------------------------------------------------------------------------

def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, timeout=60)


@pytest.fixture
def clone(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git unavailable")
    main = tmp_path / "wt repo" / "main"
    main.mkdir(parents=True)
    _git("init", "-q", cwd=main)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init", cwd=main)
    (main / "tests").mkdir()
    (main / "tests" / "conftest.py").write_text("x = 1\n", encoding="utf-8")
    wts = {}
    for name in ("wt1", "wt2"):
        wt = main / ".claude" / "worktrees" / name
        _git("worktree", "add", "-q", "--detach", str(wt), cwd=main)
        (wt / "tests").mkdir(exist_ok=True)
        wts[name] = wt
    return main, wts


def _edit(path: Path, sid: str) -> dict[str, Any]:
    return _pre("Edit", {"file_path": str(path), "old_string": "x", "new_string": "y"}, session_id=sid)


@pytest.mark.parametrize("shell", SHELLS)
def test_worktree_guard_scratch_and_plans_allowed_other_checkouts_blocked(shell, clone, tmp_path):
    main, wts = clone
    env = _clean_env(tmp_path, {"CLAUDE_PROJECT_DIR": str(wts["wt1"])})
    scratch = Path(env["TEMP"]) / "claude" / "proj" / "sess" / "scratchpad" / "x.py"
    plans = Path(env["USERPROFILE"]) / ".claude" / "plans" / "plan.md"
    for ok in (scratch, plans):
        r = _run(shell, "worktree_guard", _pre("Write", {"file_path": str(ok), "content": "x"}), env)
        assert r.returncode == 0, (ok, r.stderr[-300:])
    for bad in (main / "tests" / "conftest.py", wts["wt2"] / "tests" / "conftest.py"):
        r = _run(shell, "worktree_guard", _pre("Write", {"file_path": str(bad), "content": "x"}), env)
        assert r.returncode == 2 and b"a3984d96" in r.stderr, (bad, r.returncode)
    r = _run(shell, "worktree_guard", _pre("Write", {"file_path": str(main / "x.py"), "content": "x"}),
             dict(env, MERIDIAN_GUARD="off"))
    assert r.returncode == 0, "the owner kill switch covers the boundary block"


@pytest.mark.parametrize("shell", SHELLS)
def test_worktree_lock_is_per_checkout_and_warn_only(shell, clone, tmp_path):
    main, wts = clone
    env1 = _clean_env(tmp_path, {"CLAUDE_PROJECT_DIR": str(wts["wt1"])})
    env2 = dict(env1, CLAUDE_PROJECT_DIR=str(wts["wt2"]))
    envm = dict(env1, CLAUDE_PROJECT_DIR=str(main))
    # separate worktrees, same repo-relative path: never blocked, never warned
    r = _run(shell, "worktree_guard", _edit(wts["wt1"] / "tests" / "conftest.py", "S-wt1"), env1)
    assert r.returncode == 0 and not r.stdout.strip()
    r = _run(shell, "worktree_guard", _edit(wts["wt2"] / "tests" / "conftest.py", "S-wt2"), env2)
    assert r.returncode == 0 and not r.stdout.strip(), r.stdout
    # main tree vs a worktree: separate checkouts too
    r = _run(shell, "worktree_guard", _edit(main / "tests" / "conftest.py", "S-main"), envm)
    assert r.returncode == 0 and not r.stdout.strip(), r.stdout
    # the SAME working tree, another session within the window: allowed, warned
    r = _run(shell, "worktree_guard", _edit(main / "tests" / "conftest.py", "S-other"), envm)
    assert r.returncode == 0
    warn = _ctx(r) or ""
    assert "71f597b7" in warn and "S-main" in warn and "warning only" in warn
    # the same session again: silent
    r = _run(shell, "worktree_guard", _edit(main / "tests" / "conftest.py", "S-other"), envm)
    assert r.returncode == 0 and not r.stdout.strip()
    # an old lock (a finished session) is silently taken over
    lock = main / ".git" / "meridian-locks" / "tests" / "conftest.py.lock"
    assert lock.is_file(), "the main tree's lock lives in its own .git"
    old = time.time() - 20 * 60
    os.utime(lock, (old, old))
    r = _run(shell, "worktree_guard", _edit(main / "tests" / "conftest.py", "S-after-clear"), envm)
    assert r.returncode == 0 and not r.stdout.strip()


# ---------------------------------------------------------------------------
# sprint_guard / pkg_install_guard / test_tamper_guard against a stub server
# ---------------------------------------------------------------------------

PROJECT_ID = "5787cc92-ba7d-4788-b17c-28ab7938b839"


class _Stub:
    pending = 0
    requests: list[str] = []


class _H(BaseHTTPRequestHandler):
    def log_message(self, *_a: Any) -> None:
        pass

    def _send(self, obj: Any) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        _Stub.requests.append("GET " + self.path.split("?", 1)[0])
        if self.path.startswith(f"/projects/{PROJECT_ID}/sprint/pending_count"):
            self._send({"pending_count": _Stub.pending})
        else:
            self._send({"test_coverage_expected": False})

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        _Stub.requests.append("POST " + self.path)
        self._send({"action": "allow", "message": ""})


@pytest.fixture
def stub():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _Stub.requests = []
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def _transcript(tmp_path: Path, claimed: bool) -> Path:
    p = tmp_path / "transcript.jsonl"
    lines = [{"type": "user", "message": {"content": "please look at claim_sprint_item docs"}}]
    if claimed:
        lines.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "mcp__meridian__claim_sprint_item", "input": {"item_id": "x"}}]}})
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return p


def _stop(tp: Path | None) -> dict[str, Any]:
    d: dict[str, Any] = {"session_id": "fix-round-1", "hook_event_name": "Stop", "stop_hook_active": False}
    if tp is not None:
        d["transcript_path"] = str(tp)
    return d


@pytest.mark.parametrize("shell", SHELLS)
def test_sprint_guard_holds_back_only_a_claiming_session(shell, stub, tmp_path):
    env = _clean_env(tmp_path, {"MERIDIAN_URL": stub})
    _Stub.pending = 3
    r = _run(shell, "sprint_guard", _stop(_transcript(tmp_path, claimed=False)), env)
    assert r.returncode == 0, "a planner/Q&A session that claimed nothing stops freely"
    r = _run(shell, "sprint_guard", _stop(None), env)
    assert r.returncode == 0, "no transcript: fail open"
    assert _Stub.requests == [], "and neither asks the server"
    r = _run(shell, "sprint_guard", _stop(_transcript(tmp_path, claimed=True)), env)
    assert r.returncode == 2 and b"3 sprint item(s) still pending" in r.stderr
    r = _run(shell, "sprint_guard", _stop(_transcript(tmp_path, claimed=True)), dict(env, MERIDIAN_GUARD="off"))
    assert r.returncode == 0
    r = _run(shell, "sprint_guard", _stop(_transcript(tmp_path, claimed=True)), dict(env, MERIDIAN_GUARD="advisory"))
    assert r.returncode == 0 and b"advisory, not blocked" in r.stderr
    _Stub.pending = 0
    r = _run(shell, "sprint_guard", _stop(_transcript(tmp_path, claimed=True)), env)
    assert r.returncode == 0
    assert not any(q.startswith("POST") for q in _Stub.requests), "the destructive worktree sweep is never triggered"


@pytest.mark.parametrize("shell", SHELLS)
def test_sprint_guard_down_server_fails_open_fast(shell, tmp_path):
    env = _clean_env(tmp_path)  # MERIDIAN_URL points at a port with no listener
    t = time.monotonic()
    r = _run(shell, "sprint_guard", _stop(_transcript(tmp_path, claimed=True)), env)
    took = time.monotonic() - t
    assert r.returncode == 0 and b"41f26499" in r.stderr
    assert took < 15, took


def test_ps1_localhost_probes_fail_fast_when_the_server_is_down(tmp_path):
    """The verification run measured 4-5 s per call (Invoke-RestMethod on 'localhost'
    tries ::1 then 127.0.0.1). A 300 ms TCP probe now answers first."""
    if POWERSHELL is None:
        pytest.skip("Windows PowerShell only")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = _clean_env(tmp_path, {"MERIDIAN_URL": f"http://localhost:{port}"})
    base = time.monotonic()
    _run("ps1", "hitl_guard", _pre("Read", {"file_path": "x"}), env)  # a no-op hook: PowerShell start-up cost
    startup = time.monotonic() - base
    for hook, payload in (
        ("pkg_install_guard", _pre("Bash", {"command": "git add meridian/static/dashboard.js && npm run build"})),
        ("sprint_guard", _stop(_transcript(tmp_path, claimed=True))),
    ):
        t = time.monotonic()
        r = _run("ps1", hook, payload, env)
        took = time.monotonic() - t
        assert r.returncode == 0
        assert took < startup + 2.5, (hook, took, startup)


@pytest.mark.parametrize("shell", SHELLS)
def test_test_tamper_guard_default_mode_never_calls_the_server(shell, stub, tmp_path):
    env = _clean_env(tmp_path, {"MERIDIAN_URL": stub})
    post = {"session_id": "s", "hook_event_name": "PostToolUse", "tool_name": "Edit",
            "tool_input": {"file_path": str(REPO / "tests" / "test_core.py"), "old_string": "a", "new_string": "b"}}
    r = _run(shell, "test_tamper_guard", post, env)
    assert r.returncode == 0 and b"43539c70" in r.stderr
    assert _Stub.requests == []
    r = _run(shell, "test_tamper_guard", post, dict(env, MERIDIAN_TEST_TAMPER_BLOCK="1"))
    assert r.returncode == 2 and _Stub.requests, "block mode still consults the exemption"
    r = _run(shell, "test_tamper_guard", post, dict(env, MERIDIAN_TEST_TAMPER_BLOCK="1", MERIDIAN_GUARD="off"))
    assert r.returncode == 0
