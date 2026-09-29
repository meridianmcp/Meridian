"""The hook commands registered in ``.claude/settings.json`` actually execute.

Why this file exists (55d48d69, 2026-09-27): every ``"shell": "powershell"``
entry in this repo used to be registered as

    & "$CLAUDE_PROJECT_DIR\\.claude\\hooks\\<name>.ps1"

Claude Code runs such a hook as ``<pwsh|powershell> -NoProfile
-NonInteractive -ExecutionPolicy Bypass -Command <command>`` with
``CLAUDE_PROJECT_DIR`` in the child's ENVIRONMENT (verified in the 2.1.281
binary: ``n3t``/``tj`` build that argv; ``wL`` prefers pwsh, else Windows
PowerShell 5.1). Under PowerShell ``$CLAUDE_PROJECT_DIR`` is an unset
*variable*, so the path collapsed to ``\\.claude\\hooks\\<name>.ps1`` and every
hook failed with "is not recognized" (exit 1, non-blocking) -- about 96k such
errors in the transcripts and not one blocking result, ever. Separately,
``-Command`` turns a script's ``exit 2`` into process exit 1, so even a
correctly resolved legacy hook could never block. Every existing hook test ran
the scripts directly (``-File`` or an absolute path), never the registered
string, so none of them noticed.

These tests run each REGISTERED command string exactly the way Claude Code
does, from the project root with ``CLAUDE_PROJECT_DIR`` set, and assert that
the script ran and that its exit code reached the process -- including exit 2
for every legacy hook whose contract is to block. Network-touching hooks talk
to a local stub server (``MERIDIAN_URL``), never to a real Meridian; the guard
state/audit dir, home and Python runtime are all redirected into ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from meridian import hook_paths
from meridian import hook_settings_merge as hsm

REPO = Path(__file__).resolve().parent.parent
SETTINGS = REPO / ".claude" / "settings.json"
HOOKS_DIR = REPO / ".claude" / "hooks"
PROJECT_ID = "5787cc92-ba7d-4788-b17c-28ab7938b839"  # baked into sprint_guard/worktree_guard/test_tamper_guard


def _claude_code_powershell() -> str | None:
    """Claude Code's own resolution order (``wL`` in the 2.1.281 binary): pwsh
    first, then Windows PowerShell."""
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt":
        cand = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if cand.is_file():
            return str(cand)
    return None


POWERSHELL = _claude_code_powershell()
needs_ps = pytest.mark.skipif(POWERSHELL is None, reason="no PowerShell on this host")

_RUN_TIMEOUT_S = 90
_NOT_RECOGNIZED = b"is not recognized"


def _registered_entries() -> list[dict[str, Any]]:
    """Every hook entry in .claude/settings.json as {event, matcher, hook}."""
    data = json.loads(SETTINGS.read_text(encoding="utf-8-sig"))
    out: list[dict[str, Any]] = []
    for event, groups in (data.get("hooks") or {}).items():
        for group in groups:
            for hook in group.get("hooks") or []:
                out.append({"event": event, "matcher": group.get("matcher", ""), "hook": hook})
    return out


def _stem(command: str) -> str:
    token = hook_paths.extract_script_path_token(command)
    assert token, f"no script in registered command {command!r}"
    return hook_paths.normalize_wsl_path(token).rsplit("/", 1)[-1].rsplit(".", 1)[0]


def _claude_code_command_line(command: str) -> list[str]:
    """argv Claude Code builds for a ``"shell": "powershell"`` hook, including
    its own 2.1.281 ``${CLAUDE_PROJECT_DIR}`` -> ``${env:CLAUDE_PROJECT_DIR}``
    rewrite (``yyo``); the bare ``$CLAUDE_PROJECT_DIR`` is NOT rewritten."""
    assert POWERSHELL is not None
    for name in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA"):
        command = command.replace("${" + name + "}", "${env:" + name + "}")
    return [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command]


# ---------------------------------------------------------------------------
# Stub Meridian server (the only network peer any hook sees in these tests)
# ---------------------------------------------------------------------------


class _Stub:
    pending_count = 0


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a: Any) -> None:  # keep pytest output clean
        pass

    def _send(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == f"/projects/{PROJECT_ID}/sprint/pending_count":
            self._send({"pending_count": _Stub.pending_count, "verification_pending_count": 0})
        elif path == f"/projects/{PROJECT_ID}/sprint/test_coverage_expected":
            self._send({"test_coverage_expected": False})
        elif path == "/health":
            # hitl_guard fix round 2's reachability probe: a real, reachable Meridian
            # answers /health 2xx before the hook decides whether to block.
            self._send({"status": "ok", "service": "meridian"})
        else:
            self._send({"detail": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self.path == "/pkg-guard/check":
            self._send({"action": "warn", "message": "stub-pkg-guard: registry says this package is brand new"})
        elif self.path == f"/projects/{PROJECT_ID}/worktrees/sweep":
            self._send({})
        else:
            self._send({"detail": "not found"}, 404)


@pytest.fixture(scope="module")
def stub_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Running a registered command the way Claude Code does
# ---------------------------------------------------------------------------


def _base_env(tmp_path: Path, project_dir: Path, stub_url: str, extra: dict[str, str] | None) -> dict[str, str]:
    keep = ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP",
            "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    home = tmp_path / "home"
    lad = tmp_path / "localappdata"
    home.mkdir(exist_ok=True)
    lad.mkdir(exist_ok=True)
    env.update({
        "CLAUDE_PROJECT_DIR": str(project_dir),
        "USERPROFILE": str(home),
        "HOME": str(home),
        "LOCALAPPDATA": str(lad),
        "MERIDIAN_GUARD_DIR": str(lad / "meridian" / "guard"),
        # Deterministic brief: an explicit, missing runtime is authoritative ->
        # the brief shim prints its static fallback envelope (no Python, no network).
        "MERIDIAN_GUARD_PYTHON": str(tmp_path / "no-such-python.exe"),
        "MERIDIAN_URL": stub_url,
    })
    env.update(extra or {})
    return env


def _run_registered(command: str, payload: dict[str, Any], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    last: subprocess.CompletedProcess | None = None
    for _attempt in range(2):  # one retry: host contention has killed PowerShell starts
        try:
            last = subprocess.run(
                _claude_code_command_line(command),
                input=json.dumps(payload).encode("utf-8"),
                capture_output=True,
                env=env,
                cwd=str(cwd),
                timeout=_RUN_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            continue
        if last.returncode in (0, 1, 2):
            return last
    if last is None:
        pytest.skip("PowerShell never completed the hook process (host contention)")
    return last


# ---------------------------------------------------------------------------
# Per-hook cases: (case id, payload, extra env, expected rc, stdout check, stderr needle)
# ---------------------------------------------------------------------------

_ENV_FILE = str(REPO / ".env")  # a PATH only; nothing ever reads it


def _pre(tool: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {"session_id": "registered-cmd-test", "hook_event_name": "PreToolUse", "tool_name": tool,
            "tool_input": tool_input, "cwd": str(REPO)}


def _post(tool: str, tool_input: dict[str, Any], response: Any = "") -> dict[str, Any]:
    return {"session_id": "registered-cmd-test", "hook_event_name": "PostToolUse", "tool_name": tool,
            "tool_input": tool_input, "tool_response": response, "cwd": str(REPO)}


def _json_envelope(event: str, needle: str | None = None):
    def check(stdout: bytes) -> None:
        obj = json.loads(stdout.decode("utf-8"))
        hso = obj["hookSpecificOutput"]
        assert hso["hookEventName"] == event
        if needle is not None:
            assert needle in hso["additionalContext"]
    return check


def _deny(stdout: bytes) -> None:
    obj = json.loads(stdout.decode("utf-8"))
    hso = obj["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"


def _empty_or_json(stdout: bytes) -> None:
    text = stdout.decode("utf-8").strip()
    if text:
        json.loads(text)


def _empty(stdout: bytes) -> None:
    assert stdout.strip() == b"", stdout[-500:]


# Keyed by script stem. Every stem registered in .claude/settings.json MUST have
# at least one case (test_every_registered_hook_has_a_case), so a new hook entry
# cannot slip in untested.
CASES: dict[str, list[tuple[str, dict[str, Any], dict[str, str], int, Any, str | None]]] = {
    "hitl_guard": [
        ("blocks_native_ask", _pre("AskUserQuestion", {"questions": []}), {}, 2, _empty, "HITL guard"),
    ],
    "worktree_guard": [
        # main-tree session editing a path outside the project: unconditional allow
        ("outside_main_tree_allows", _pre("Write", {"file_path": "C:/nonexistent-meridian-test/x.txt", "content": "x"}),
         {}, 0, _empty, None),
    ],
    "secret_guard": [
        ("blocks_read_of_env_file", _pre("Read", {"file_path": _ENV_FILE}), {}, 2, _empty, "secret guard"),
        ("allows_read_of_readme", _pre("Read", {"file_path": str(REPO / "README.md")}), {}, 0, _empty, None),
        # 55d48d69 fix round 1: source files named *secret* / *_token.* are not credentials
        ("allows_read_of_secret_redaction_source", _pre("Read", {"file_path": str(REPO / "meridian" / "secret_redaction.py")}),
         {}, 0, _empty, None),
        ("allows_shell_prologue", _pre("Bash", {"command": "set -euo pipefail; pixi run test"}), {}, 0, _empty, None),
    ],
    "dependency_install_guard": [
        ("blocks_undeclared_pip_install",
         _pre("Bash", {"command": "pip install zz-meridian-registered-cmd-test-not-a-real-pkg"}), {}, 2, _empty, None),
        ("allows_non_install", _pre("Bash", {"command": "git status"}), {}, 0, _empty, None),
    ],
    "pkg_install_guard": [
        # exit 1 is this hook's documented "warn, do not block" contract
        ("warn_exit_1_survives", _pre("Bash", {"command": "pip install requests"}), {}, 1, _empty, "stub-pkg-guard"),
        ("allows_non_install", _pre("Bash", {"command": "git status"}), {}, 0, _empty, None),
    ],
    "meridian_guard": [
        # G6: an auto-memory write is denied (JSON decision, exit 0 -- the guard never exits 2)
        ("g6_denies_auto_memory_write", None, {}, 0, _deny, None),
    ],
    "test_tamper_guard": [
        ("flags_test_edit_nonblocking", _post("Edit", {"file_path": str(REPO / "tests" / "test_x.py"),
                                                      "old_string": "a", "new_string": "b"}),
         {}, 0, _empty, "test-tamper guard"),
        ("opt_in_block_exit_2_survives", _post("Edit", {"file_path": str(REPO / "tests" / "test_x.py"),
                                                       "old_string": "a", "new_string": "b"}),
         {"MERIDIAN_TEST_TAMPER_BLOCK": "1"}, 2, _empty, "test-tamper guard"),
    ],
    "meridian_guard_post": [
        ("post_runs_silently_or_injects", _post("WebSearch", {"query": "x"}, {"results": []}), {}, 0, _empty_or_json, None),
    ],
    "sprint_guard": [
        ("stop_hook_active_allows", {"session_id": "registered-cmd-test", "hook_event_name": "Stop",
                                     "stop_hook_active": True}, {}, 0, _empty, None),
        # 55d48d69 fix round 1: only a session that claimed a sprint item is held back
        ("blocks_stop_with_pending_items", {"session_id": "registered-cmd-test", "hook_event_name": "Stop",
                                            "stop_hook_active": False},
         {"_PENDING": "2", "_CLAIMED_TRANSCRIPT": "1"}, 2, _empty, "2 sprint item(s) still pending"),
        ("non_executor_session_stops_freely", {"session_id": "registered-cmd-test", "hook_event_name": "Stop",
                                               "stop_hook_active": False}, {"_PENDING": "2"}, 0, _empty, None),
        ("allows_stop_with_nothing_pending", {"session_id": "registered-cmd-test", "hook_event_name": "Stop",
                                              "stop_hook_active": False},
         {"_PENDING": "0", "_CLAIMED_TRANSCRIPT": "1"}, 0, _empty, None),
    ],
    "post_compact_refresh": [
        ("compact_injects_reminder", {"session_id": "registered-cmd-test", "hook_event_name": "SessionStart",
                                      "source": "compact"}, {}, 0,
         _json_envelope("SessionStart", "Context was just compacted"), None),
    ],
    "meridian_guard_brief": [
        ("session_start_fallback_brief", {"session_id": "registered-cmd-test", "hook_event_name": "SessionStart",
                                          "source": "startup"}, {}, 0,
         _json_envelope("SessionStart", "static fallback"), None),
        ("subagent_start_fallback_brief", {"session_id": "registered-cmd-test", "hook_event_name": "SubagentStart",
                                           "agent_type": "general-purpose"}, {}, 0,
         _json_envelope("SubagentStart", "static fallback"), None),
    ],
}


def _event_ok(stem: str, case_payload: dict[str, Any] | None, event: str) -> bool:
    if case_payload is None:
        return event == "PreToolUse"
    return case_payload.get("hook_event_name") == event


def _params() -> list[Any]:
    params = []
    for idx, entry in enumerate(_registered_entries()):
        hook = entry["hook"]
        if hook.get("type") != "command" or hook.get("shell") != "powershell":
            continue
        stem = _stem(hook["command"])
        for case_id, payload, extra, rc, check, needle in CASES.get(stem, []):
            if not _event_ok(stem, payload, entry["event"]):
                continue
            params.append(pytest.param(entry, payload, extra, rc, check, needle,
                                       id=f"{idx}-{entry['event']}-{stem}-{case_id}"))
    return params


# ---------------------------------------------------------------------------
# Structural (no subprocess)
# ---------------------------------------------------------------------------

_BARE_PS_VAR = re.compile(r"(?<!env:)\$\{?CLAUDE_PROJECT_DIR\b")


def test_no_registered_powershell_command_uses_the_bare_variable_form():
    """``$CLAUDE_PROJECT_DIR`` / ``${CLAUDE_PROJECT_DIR}`` inside a PowerShell
    command is an unset PowerShell variable, not the environment variable."""
    ps = [e["hook"]["command"] for e in _registered_entries() if e["hook"].get("shell") == "powershell"]
    assert ps, "expected PowerShell hook entries in .claude/settings.json"
    offenders = [c for c in ps if _BARE_PS_VAR.search(c)]
    assert offenders == [], offenders
    for c in ps:
        assert "$env:CLAUDE_PROJECT_DIR" in c, c


def test_every_registered_powershell_command_propagates_exit_codes():
    for e in _registered_entries():
        hook = e["hook"]
        if hook.get("shell") != "powershell":
            continue
        assert hook["command"].endswith(hsm.PS_EXIT_SUFFIX), hook["command"]
        # it is exactly what the installer's repair would produce (a fixed point)
        assert hsm.repair_powershell_command(hook["command"]) == hook["command"]


def test_every_registered_hook_has_a_case():
    stems = {(_stem(e["hook"]["command"]), e["event"]) for e in _registered_entries()
             if e["hook"].get("shell") == "powershell"}
    missing = []
    for stem, event in sorted(stems):
        if not any(_event_ok(stem, p, event) for _cid, p, *_rest in CASES.get(stem, [])):
            missing.append((stem, event))
    assert missing == [], f"registered hooks without a registered-command case: {missing}"


def test_registered_scripts_exist_and_resolve_via_hook_paths():
    for e in _registered_entries():
        diag = hook_paths.resolve_configured_hook_command(e["hook"]["command"], REPO)
        assert diag["required"] is True, diag
        assert diag["status"] == hook_paths.STATUS_OK, diag


def test_settings_is_what_the_installer_generates():
    """Regenerating this file with the installer's merge + launcher repair is a
    no-op: the committed file IS the installer's output."""
    doc = hsm.load_settings(SETTINGS)
    desired = hsm.desired_entries(scope="project", mode="enforce", shell="powershell")
    merged, changed = hsm.merge_owned(doc.data, desired)
    repaired_data, repaired = hsm.repair_powershell_launchers(merged)
    assert not changed and repaired == []
    assert doc.render(repaired_data) == doc.original_text


# ---------------------------------------------------------------------------
# Subprocess: run every registered command exactly as Claude Code does
# ---------------------------------------------------------------------------


@needs_ps
@pytest.mark.subprocess_isolated
@pytest.mark.timeout(400)
@pytest.mark.parametrize("entry,payload,extra,expected_rc,check,needle", _params())
def test_registered_command_executes(entry, payload, extra, expected_rc, check, needle, tmp_path, stub_url):
    extra = dict(extra)
    _Stub.pending_count = int(extra.pop("_PENDING", "0"))
    if extra.pop("_CLAIMED_TRANSCRIPT", None) and payload is not None:
        tp = tmp_path / "transcript.jsonl"
        tp.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "mcp__meridian__claim_sprint_item", "input": {}}]}}) + "\n",
            encoding="utf-8")
        payload = dict(payload, transcript_path=str(tp))
    if payload is None:  # G6 memory write, rooted in this test's own home dir
        payload = _pre("Write", {"file_path": str(tmp_path / "home" / ".claude" / "projects" / "p" / "memory" / "a.md"),
                                 "content": "x"})
    env = _base_env(tmp_path, REPO, stub_url, extra)
    command = entry["hook"]["command"]
    r = _run_registered(command, payload, env, REPO)
    assert _NOT_RECOGNIZED not in r.stderr, r.stderr[-2000:]
    assert r.returncode == expected_rc, (r.returncode, r.stdout[-1000:], r.stderr[-2000:])
    check(r.stdout)
    if needle is not None:
        assert needle.encode() in r.stderr, r.stderr[-2000:]


@needs_ps
@pytest.mark.subprocess_isolated
@pytest.mark.timeout(400)
def test_worktree_guard_block_survives_in_a_path_with_spaces(tmp_path, stub_url):
    """A worktree session (``.../.claude/worktrees/<name>``) under a directory
    with spaces: the registered command resolves the script from the
    environment and the guard's exit 2 reaches Claude Code."""
    entry = next(e for e in _registered_entries() if "worktree_guard" in e["hook"]["command"])
    project = tmp_path / "main repo with spaces" / ".claude" / "worktrees" / "wt one"
    shutil.copytree(HOOKS_DIR, project / ".claude" / "hooks",
                    ignore=shutil.ignore_patterns("*.sh", "*.awk"))
    payload = _pre("Write", {"file_path": str(tmp_path / "elsewhere" / "x.py"), "content": "x"})
    env = _base_env(tmp_path, project, stub_url, None)
    r = _run_registered(entry["hook"]["command"], payload, env, project)
    assert _NOT_RECOGNIZED not in r.stderr, r.stderr[-2000:]
    assert r.returncode == 2, (r.returncode, r.stderr[-2000:])
    assert b"worktree guard" in r.stderr


@needs_ps
@pytest.mark.subprocess_isolated
@pytest.mark.timeout(400)
def test_before_the_fix_the_registered_form_never_ran(tmp_path, stub_url):
    """Regression evidence: the old registered string, run the same way,
    fails with "is not recognized" (exit 1) -- the hook never executes -- and
    the brace spelling under Claude Code's own rewrite runs the script but
    loses its exit 2."""
    payload = _pre("Read", {"file_path": _ENV_FILE})
    env = _base_env(tmp_path, REPO, stub_url, None)
    old = '& "$CLAUDE_PROJECT_DIR\\.claude\\hooks\\secret_guard.ps1"'
    r = _run_registered(old, payload, env, REPO)
    assert r.returncode == 1 and _NOT_RECOGNIZED in r.stderr, (r.returncode, r.stderr[-1000:])
    brace = '& "${CLAUDE_PROJECT_DIR}\\.claude\\hooks\\secret_guard.ps1"'
    r = _run_registered(brace, payload, env, REPO)
    assert _NOT_RECOGNIZED not in r.stderr and b"secret guard" in r.stderr
    assert r.returncode == 1, r.returncode  # -Command collapsed the script's exit 2
    fixed = hsm.repair_powershell_command(old)
    r = _run_registered(fixed, payload, env, REPO)
    assert r.returncode == 2 and b"secret guard" in r.stderr, (r.returncode, r.stderr[-1000:])


# Launcher semantics on synthetic scripts: exit codes pass through, and a crash
# never becomes a block (2).
_SYNTHETIC = {
    "exit2": ("[Console]::Error.WriteLine('reason'); exit 2\n", 2),
    "exit1": ("exit 1\n", 1),
    "exit0": ("exit 0\n", 0),
    "falls_off_end_after_native_exit_2": ("cmd /c exit 2\n", 0),
    "throws": ("throw 'boom'\n", 1),
    "parse_error": ("$x = (1 +\n", 1),
    "stop_error": ("$ErrorActionPreference = 'Stop'; Get-Item -LiteralPath 'C:/no/such/meridian/path' | Out-Null\n", 1),
    "native_2_then_throw": ("cmd /c exit 2\nthrow 'x'\n", 1),
    "non_terminating_error": ("Write-Error 'nt'\n", 0),
}


@needs_ps
@pytest.mark.skipif(os.name != "nt", reason="uses cmd.exe for a native exit code")
@pytest.mark.subprocess_isolated
@pytest.mark.timeout(400)
@pytest.mark.parametrize("name", sorted(_SYNTHETIC))
def test_launcher_exit_code_semantics(name, tmp_path, stub_url):
    body, expected = _SYNTHETIC[name]
    project = tmp_path / "proj with space"
    hooks = project / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "probe.ps1").write_text(body, encoding="ascii")
    command = hsm.ps_launch(hsm.ps_project_script(".claude\\hooks\\probe.ps1"))
    r = _run_registered(command, {}, _base_env(tmp_path, project, stub_url, None), project)
    assert r.returncode == expected, (name, r.returncode, r.stderr[-1000:])
    assert _NOT_RECOGNIZED not in r.stderr


@needs_ps
@pytest.mark.subprocess_isolated
@pytest.mark.timeout(400)
def test_missing_script_is_a_visible_non_blocking_error(tmp_path, stub_url):
    command = hsm.ps_launch(hsm.ps_project_script(".claude\\hooks\\no_such_hook.ps1"))
    r = _run_registered(command, {}, _base_env(tmp_path, tmp_path, stub_url, None), tmp_path)
    # The exit code is the load-bearing assertion (PS_EXIT_SUFFIX derives it from
    # PowerShell's own $?/$LASTEXITCODE, not from OS/version-specific error text),
    # so it stays pinned at 1 (visible, non-blocking) on every PowerShell version.
    #
    # The message WORDING for "this script doesn't exist" genuinely differs by
    # platform, confirmed 2026-09-29: Windows PowerShell 5.1 resolves the
    # backslash-separated registered path as a filesystem path and reports
    # CommandNotFoundException as "...is not recognized as the name of a
    # cmdlet, function, script file, or operable program" (_NOT_RECOGNIZED).
    # CI's ubuntu-latest runner uses `pwsh` (PowerShell Core) instead, where `\`
    # is not a path separator -- the registered ".claude\\hooks\\no_such_hook.ps1"
    # form (baked in for the Windows boxes these hooks actually run on) no longer
    # parses as a filesystem path at all, so pwsh's command discovery instead
    # treats the trailing "...\no_such_hook.ps1" segment as a module-qualified
    # command reference (PowerShell's `Module\Command` syntax) and reports a
    # distinct "... module ... could not be loaded ..." CommandNotFoundException
    # instead. Both are the SAME underlying event (the registered command
    # references a script that doesn't exist) surfacing through a genuinely
    # different, version-dependent PowerShell code path -- not a behavior
    # regression -- so this accepts either phrasing rather than pinning one
    # platform's exact text.
    assert r.returncode == 1, (r.returncode, r.stdout[-500:], r.stderr[-1000:])
    stderr_lower = r.stderr.lower()
    assert (
        _NOT_RECOGNIZED in r.stderr
        or (b"module" in stderr_lower and b"could not be loaded" in stderr_lower)
    ), r.stderr[-1000:]


@needs_ps
@pytest.mark.subprocess_isolated
@pytest.mark.timeout(400)
def test_user_scope_command_propagates_exit_codes(tmp_path, stub_url):
    """The user-scope form (absolute paths, defer check first) runs the shim
    and keeps its exit code; the defer check short-circuits to exit 0."""
    user_hooks = tmp_path / "home dir" / ".claude" / "hooks"
    user_hooks.mkdir(parents=True)
    (user_hooks / "meridian_guard_defer.ps1").write_text(hsm.DEFER_PS1, encoding="ascii")
    (user_hooks / "meridian_guard.ps1").write_text(
        "if ($env:MERIDIAN_GUARD_SCOPE -ne 'user') { exit 3 }\n[Console]::Error.WriteLine('probe'); exit 2\n",
        encoding="ascii")
    command = hsm.build_command("meridian_guard", scope="user", mode="enforce", shell="powershell",
                                user_hooks_dir=user_hooks)
    project = tmp_path / "some project"
    (project / ".claude").mkdir(parents=True)
    env = _base_env(tmp_path, project, stub_url, None)
    r = _run_registered(command, {}, env, project)
    assert r.returncode == 2 and b"probe" in r.stderr, (r.returncode, r.stderr[-1000:])
    # the project registers its own guard -> the user-scope entry defers (exit 0, silent)
    (project / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "shell": "powershell",
         "command": hsm.build_command("meridian_guard", scope="project", mode="enforce", shell="powershell")}]}]}}),
        encoding="utf-8")
    r = _run_registered(command, {}, env, project)
    assert (r.returncode, r.stderr) == (0, b""), (r.returncode, r.stderr[-1000:])
