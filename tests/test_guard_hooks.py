"""55d48d69 -- the Meridian guard hook shims vs the Python decision core.

meridian/guard_core.py is the SPEC. The hot-path hooks mirror it natively:

* ``.claude/hooks/meridian_guard.ps1``       PreToolUse  G0-G11 (PowerShell 5.1+)
* ``.claude/hooks/meridian_guard_post.ps1``  PostToolUse G12-G14 (wrapper: ``-Mode post``)
* ``.claude/hooks/meridian_guard.sh``        PreToolUse  (bash wrapper + POSIX awk engine
  ``.claude/hooks/meridian_guard.awk``)
* ``.claude/hooks/meridian_guard_post.sh``   PostToolUse (sources the sh shim with ``--post``)

This module runs the REAL scripts as subprocesses against EVERY case in
``tests/fixtures/guard_cases.json`` and asserts parity with ``guard_core`` on
the very same inputs:

* each case is materialized on a real temp filesystem by the fixture's own
  ``path_roots`` contract (``C:/Users/13144`` and ``D:/`` relocated by a
  case/separator-insensitive prefix rewrite; the msys ``/c/Users/13144``
  spelling too on Windows) and its clock is shifted onto real time (db
  mtimes, ``indexed_epoch`` and state timestamps move by ``now - case.now``);
* ``guard_core.evaluate`` is computed on those relocated inputs with the real
  filesystem, then the shim runs; decision, rule, project, shadowed list,
  reason text, stdout JSON, the per-session state file and the audit line must
  all match, and the relocated fixture expectations must hold as well;
* ``EXTRA_CASES`` (parity-only, the core is the oracle) add what the ASCII
  fixture cannot show: code points vs UTF-16 units vs UTF-8 bytes, Unicode
  word boundaries/whitespace, json.dumps of dict responses, str() of
  non-string JSON and megabyte payloads;
* to keep 2 x 396 cases affordable the parity sweep uses each shim's batch
  entry point (``-Batch DIR`` / ``--batch MANIFEST FACTS``: the same engine,
  one process per chunk of cases, which refuses the live guard dir). The
  replay calls D1-D8/A1-A12, the fail-safe inputs (bad stdin, missing/corrupt
  snapshot, unreadable state file, an engine that fails while loading), the
  kill-switch flip and a deny -> receipt -> retry sequence additionally run one
  real hook invocation per call (stdin -> stdout), for the ps1 also in the exact
  ``& "<path>"`` form ``.claude/settings.json`` registers (on Windows the sh
  replay runs a representative subset: Git Bash spawns cost seconds there, and
  the batch sweep already covers every case).

SessionStart/SubagentStart cases belong to the brief shim (meridian_guard_brief,
a separate track); here they only prove that these two shims stay silent for a
foreign event. The ps1 tests run on Windows only (Windows PowerShell is the
ps1 shim's runtime); the sh tests run wherever bash is available.

Every subprocess test is ``subprocess_isolated`` and retries on the known
host-contention failure modes (timeouts, Windows NTSTATUS crash exits, MSYS2
fork/heap errors on stderr) inside a per-test subprocess budget that stays below
the test's pytest-timeout; when a host never produces a result the test is
skipped with that reason instead of failing -- a real mismatch always fails.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from meridian import cbm_registry as reg
from meridian import guard_core as gc

# Every test here spawns real shells. Under host contention (many concurrent agent
# sessions) one Git Bash hook call has taken 13 s and one PowerShell batch chunk far
# longer, and pytest-timeout's thread method on Windows ends the WHOLE pytest process
# when a test overruns -- turning a slow host into a dead serial step. So each test gets
# a generous pytest-timeout ceiling and, inside it, a hard subprocess DEADLINE
# (``_TEST_BUDGET_S``, see ``_deadline``): every subprocess timeout is clipped to the
# time left, and a test whose budget runs out skips ("host never answered") instead of
# failing. A real mismatch always fails.
_TEST_BUDGET_S = 150.0
_TEST_TIMEOUT_S = 240
pytestmark = [pytest.mark.subprocess_isolated, pytest.mark.timeout(_TEST_TIMEOUT_S)]

_REPO = Path(__file__).resolve().parent.parent
_HOOKS = _REPO / ".claude" / "hooks"
PS1 = _HOOKS / "meridian_guard.ps1"
PS1_POST = _HOOKS / "meridian_guard_post.ps1"
SH = _HOOKS / "meridian_guard.sh"
SH_POST = _HOOKS / "meridian_guard_post.sh"
AWK = _HOOKS / "meridian_guard.awk"
SHIM_FILES = (PS1, PS1_POST, SH, SH_POST, AWK)

FIXTURE = Path(__file__).parent / "fixtures" / "guard_cases.json"
DOC = json.loads(FIXTURE.read_text(encoding="utf-8"))
CASES = DOC["cases"]
NOW = DOC["now"]
IS_WIN = os.name == "nt"

_WIN_CRASH_CODES = frozenset({
    0xC0000005 & 0xFFFFFFFF,  # ACCESS_VIOLATION
    0xC000007B & 0xFFFFFFFF,  # INVALID_IMAGE_FORMAT
    0xC0000135 & 0xFFFFFFFF,  # DLL_NOT_FOUND
    0xC0000142 & 0xFFFFFFFF,  # DLL_INIT_FAILED
    0xC000013A & 0xFFFFFFFF,  # CONTROL_C_EXIT / kill
    3221225773,               # observed under -n auto
})
_CHUNK = 24  # cases per batch process; one chunk must fit the suite's 60 s per-test timeout
_BATCH_TIMEOUT_S = 1200
_RUN_TIMEOUT_S = 180
_REPLAY = [c["name"] for c in CASES if c.get("source") == "replay-2026-09-26"]
# Cases whose fixture expectation depends on Windows path semantics (a
# case-insensitive filesystem, the msys /c/ spelling). Off Windows they still
# get the full shim-vs-core parity check, only the relocated expectation is skipped.
_WINDOWS_ONLY_EXPECTATION = {"G1_lowercase_input_path", "G6_msys_spelling"}


# ---------------------------------------------------------------------------
# Extra parity-only cases: the fixture is ASCII-only and small, so these add the
# inputs where a hand-port most easily drifts from Python (code points vs UTF-16
# units vs UTF-8 bytes, Unicode word boundaries and whitespace, json.dumps of
# dict responses incl. float repr, str() of non-string JSON, megabyte payloads).
# guard_core is the oracle; they carry no hand-written expectation.
# ---------------------------------------------------------------------------

_XR = "C:/Users/13144/Documents/Meridian/repository"
_XSLUG = "C-Users-13144-Documents-Meridian-repository"


def _xpre(tool: str, ti: dict) -> dict:
    return {"session_id": "extra", "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": ti,
            "cwd": _XR.replace("/", "\\")}


def _xpost(tool: str, resp: Any, ti: dict | None = None, event: str = "PostToolUse") -> dict:
    return {"session_id": "extra", "hook_event_name": event, "tool_name": tool, "tool_input": ti or {},
            "tool_response": resp, "cwd": _XR.replace("/", "\\")}


def _x(name: str, event: str, payload: dict, **kw: Any) -> dict:
    c = {"name": name, "group": "extra", "source": "extra", "event": event, "payload": payload,
         "snapshot_key": "indexed", "fs_key": "machine_2026_09_26", "env": {}, "parity_only": True}
    c.update(kw)
    return c


_BIG_DICT = {"items": ["x" * 100] * 700, "pi": 3.14159, "tiny": 1e-7, "big": 1e22, "whole": 2.0,
             "t": True, "n": None, "u": "\u00e9\u65e5\U0001F600", "int": 1234567890123, "neg": -1.5}
EXTRA_CASES = [
    _x("X_unicode_pattern_grep", "PreToolUse", _xpre("Grep", {"pattern": "z\u00fcrich|\u65e5\u672c\u8a9e", "path": _XR})),
    _x("X_long_emoji_pattern_truncation", "PreToolUse",
       _xpre("Grep", {"pattern": "\U0001F600" * 30 + " abc_def " + "\u00e9" * 60, "path": _XR})),
    _x("X_unicode_path_bash_grep", "PreToolUse", _xpre("Bash", {"command": 'grep -rn "caf\u00e9" "' + _XR + '/meridian"'})),
    _x("X_crlf_command", "PreToolUse", _xpre("Bash", {"command": "cd " + _XR + "\r\ngrep -rn arxiv_search meridian\r\n"})),
    _x("X_astral_response_under_limit", "PostToolUse", _xpost("mcp__meridian__get_sprint_items", "\U0001F600" * 31000 + "x")),
    _x("X_multibyte_response_over_limit", "PostToolUse", _xpost("mcp__meridian__get_sprint_items", "\u00e9" * 70000)),
    _x("X_smart_quoted_directive", "PostToolUse", _xpost("mcp__meridian__start_session", "\u201cno_confirmation\u201d: true")),
    _x("X_word_glued_directive_is_not_one", "PostToolUse",
       _xpost("mcp__meridian__load_handoff", "\u00e9no_confirmation and xOVERRIDE and OVERRIDE_x")),
    _x("X_dict_response_dumps_length", "PostToolUse", _xpost("mcp__meridian__get_sprint_items", _BIG_DICT)),
    _x("X_dict_response_directive", "PostToolUse",
       _xpost("mcp__meridian__claim_sprint_item", {"a": {"b": ["execute_immediately"]}, "k": 1.5})),
    _x("X_mixed_list_response", "PostToolUse",
       _xpost("mcp__meridian__load_handoff", [1, {"type": "text", "text": "OVERRIDE now"}, None, [1, "two"], "plain"])),
    _x("X_nbsp_error_head", "PostToolUse",
       _xpost("mcp__codebase-memory-mcp__search_graph", "\u00a0\u2003Error: 503 Service Unavailable", {"project": _XSLUG}),
       state={"code_receipts": [[NOW - 100, False, None]]}),
    _x("X_non_string_query_list", "PreToolUse", _xpre("WebSearch", {"query": ["papers on sparse autoencoders"]}),
       state={"research_receipts": [[NOW - 60, True]]}),
    _x("X_large_write_allowed", "PreToolUse", _xpre("Write", {"file_path": _XR + "/docs/big.md", "content": "line \u00e9\n" * 150000})),
    _x("X_large_write_to_memory_denied", "PreToolUse",
       _xpre("Write", {"file_path": "C:/Users/13144/.claude/projects/p/memory/big.md", "content": "y" * 1000000})),
    _x("X_bool_tool_response", "PostToolUse", _xpost("mcp__meridian__start_session", True)),
    _x("X_number_tool_response", "PostToolUse", _xpost("mcp__meridian__start_session", 1.5e300)),
    _x("X_unicode_memory_path_via_home_var", "PreToolUse",
       _xpre("Edit", {"file_path": "$env:USERPROFILE\\.claude\\projects\\\u00e9t\u00e9\\memory\\n.md", "old_string": "a", "new_string": "b"})),
]
ALL_CASES = CASES + EXTRA_CASES


def _find_powershell() -> str | None:
    """Windows PowerShell only: the ps1 shim is what Windows hosts register
    (``shell: powershell``); POSIX hosts register the sh shim. pwsh on Linux parses
    JSON differently (ConvertFrom-Json turns ISO dates into DateTime) and is not a
    supported runtime for it, so off Windows the ps1 tests skip."""
    if not IS_WIN:
        return None
    for name in ("powershell", "pwsh"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _find_bash() -> str | None:
    p = shutil.which("bash")
    if not p:
        return None
    if IS_WIN and "system32" in p.lower():
        return None  # the WSL launcher, not Git Bash: it cannot open the Windows temp roots
    return p


POWERSHELL = _find_powershell()
BASH = _find_bash()
needs_ps = pytest.mark.skipif(POWERSHELL is None or not PS1.exists(), reason="PowerShell or meridian_guard.ps1 unavailable")
needs_bash = pytest.mark.skipif(BASH is None or not SH.exists(), reason="bash (Git Bash on Windows) or meridian_guard.sh unavailable")


# ---------------------------------------------------------------------------
# Relocation onto a real filesystem + clock shift
# ---------------------------------------------------------------------------

_ROOT_RE = re.compile(
    r"(?P<c>C:[\\/]+Users[\\/]+13144)"
    r"|(?<![A-Za-z0-9])(?P<d>D:)(?P<dsep>[\\/])"
    r"|(?<![A-Za-z0-9_.-])(?P<m>/c/Users/13144)",
    re.I,
)


class _Reloc:
    """The fixture's path_roots rewrite: C:/Users/13144 and D:/ -> under ``root``."""

    def __init__(self, root: Path):
        base = str(root).replace("\\", "/").rstrip("/")
        self.c_fwd = base + "/C/Users/13144"
        self.d_fwd = base + "/D"
        m = re.match(r"^([A-Za-z]):(/.*)$", self.c_fwd)
        self.msys = ("/" + m.group(1).lower() + m.group(2)) if m else None

    def _sub(self, m: re.Match) -> str:
        if m.group("c"):
            return self.c_fwd.replace("/", "\\") if "\\" in m.group("c") else self.c_fwd
        if m.group("d"):
            sep = m.group("dsep")
            return (self.d_fwd.replace("/", "\\") if sep == "\\" else self.d_fwd) + sep
        return self.msys if self.msys else m.group(0)

    def s(self, text: str) -> str:
        return _ROOT_RE.sub(self._sub, text)

    def obj(self, o: Any) -> Any:
        if isinstance(o, str):
            return self.s(o)
        if isinstance(o, list):
            return [self.obj(x) for x in o]
        if isinstance(o, dict):
            return {self.s(k) if isinstance(k, str) else k: self.obj(v) for k, v in o.items()}
        return o


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _reloc_snapshot(snap: Any, rl: _Reloc, delta: int) -> Any:
    s = rl.obj(snap)
    if not isinstance(s, dict):
        return s
    if isinstance(s.get("rows"), list):
        for r in s["rows"]:
            if isinstance(r, dict):
                if isinstance(r.get("root_key"), str):
                    r["root_key"] = r["root_key"].lower()
                if _num(r.get("indexed_epoch")) and r["indexed_epoch"]:
                    r["indexed_epoch"] = r["indexed_epoch"] + delta
    if isinstance(s.get("pins"), dict):
        s["pins"] = {k.lower(): v for k, v in s["pins"].items()}
    srv = s.get("servers")
    if isinstance(srv, dict) and isinstance(srv.get("projects"), dict):
        srv["projects"] = {k.lower(): v for k, v in srv["projects"].items()}
    return s


def _reloc_state(state: Any, rl: _Reloc, delta: int) -> Any:
    st = rl.obj(state)
    if not isinstance(st, dict):
        return st
    for key in ("code_receipts", "research_receipts"):
        if isinstance(st.get(key), list):
            for r in st[key]:
                if isinstance(r, list) and r and _num(r[0]):
                    r[0] = r[0] + delta
    if isinstance(st.get("capture_receipts"), list):
        st["capture_receipts"] = [t + delta if _num(t) else t for t in st["capture_receipts"]]
    for key in ("degraded_until", "web_reminder_at"):
        if _num(st.get(key)) and st[key]:
            st[key] = st[key] + delta
    if isinstance(st.get("advisory_seen"), dict):
        st["advisory_seen"] = {k.lower(): (v + delta if _num(v) else v) for k, v in st["advisory_seen"].items()}
    return st


def _merge_fs(base: dict, overlay: dict | None) -> dict:
    out = {"dirs": list(base.get("dirs") or []), "files": dict(base.get("files") or {}), "mtimes": dict(base.get("mtimes") or {})}
    for k in ("dirs",):
        out[k] += list((overlay or {}).get(k) or [])
    out["files"].update((overlay or {}).get("files") or {})
    out["mtimes"].update((overlay or {}).get("mtimes") or {})
    return out


def _case_env(case: dict) -> dict:
    env = dict(DOC["base_env"])
    for k, v in (case.get("env") or {}).items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return env


def _mode_for(case: dict) -> str | None:
    if case["event"] == "PreToolUse":
        return "pre"
    if case["event"] in ("PostToolUse", "PostToolUseFailure"):
        return "post"
    return None  # SessionStart / SubagentStart / other: the brief shim's events


def _np(p: str | os.PathLike) -> str:
    return reg.norm_path(str(p)) or str(p)


def _materialize(case: dict, root: Path, t0: float) -> dict:
    """Build the case's filesystem under ``root`` and return its relocated inputs."""
    rl = _Reloc(root)
    delta = int(round(t0 - case.get("now", NOW)))
    spec = _merge_fs(DOC["filesystems"][case["fs_key"]], case.get("fs_overlay"))
    for d in spec["dirs"]:
        Path(rl.s(d)).mkdir(parents=True, exist_ok=True)
    for f, content in spec["files"].items():
        p = Path(rl.s(f))
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write(rl.s(content) if isinstance(content, str) else "")
    for f, m in spec["mtimes"].items():
        p = Path(rl.s(f))
        if p.exists():
            os.utime(p, (m + delta, m + delta))
    env = rl.obj(_case_env(case))
    payload = rl.obj(case["payload"])
    gdir = reg.guard_dir(env)
    assert gdir, "every fixture env yields a guard dir"
    snap_obj = DOC["snapshots"][case["snapshot_key"]]
    snap_path = Path(gdir) / "snapshot.json"
    snapshot = None
    if snap_obj is None:
        if snap_path.exists():
            snap_path.unlink()
    else:
        snapshot = _reloc_snapshot(snap_obj, rl, delta)
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(json.dumps(snapshot), encoding="utf-8")
    sid = gc._safe_session(payload.get("session_id") if isinstance(payload, dict) else None)
    state_path = Path(gdir) / "state" / f"{sid}.json"
    state = None
    if "state" in case:
        state = _reloc_state(case["state"], rl, delta)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state), encoding="utf-8")
    audit_path = Path(gdir) / "audit.log"
    return {
        "case": case, "root": root, "rl": rl, "env": env, "payload": payload, "payload_text": json.dumps(payload),
        "snapshot": snapshot, "state": state, "gdir": gdir, "sid": sid, "state_path": state_path,
        "audit_path": audit_path, "mode": _mode_for(case) or "pre", "in_scope": _mode_for(case) is not None,
        "state_before": state_path.read_text(encoding="utf-8") if state_path.exists() else None,
        "audit_before": audit_path.read_text(encoding="utf-8") if audit_path.exists() else "",
        "contains": [rl.s(x) for x in case.get("expected_reason_contains", [])],
        "excludes": [rl.s(x) for x in case.get("expected_reason_excludes", [])],
    }


def _reference(mat: dict) -> dict:
    """guard_core's answer for what this shim sees (same event routing as the shims)."""
    payload = mat["payload"]
    ev = "PreToolUse" if mat["mode"] == "pre" else "PostToolUse"
    if isinstance(payload, dict):
        hen = payload.get("hook_event_name")
        if mat["mode"] == "pre":
            if isinstance(hen, str) and hen and hen != "PreToolUse":
                return {"decision": "allow", "rule_id": None, "reason": "foreign event", "_event": ev}
        elif isinstance(hen, str) and hen in ("PostToolUse", "PostToolUseFailure"):
            ev = hen
        elif isinstance(hen, str) and hen:
            return {"decision": "allow", "rule_id": None, "reason": "foreign event", "_event": ev}
    res = gc.evaluate(ev, payload, mat["snapshot"], mat["state"], mat["env"], now=time.time())
    res["_event"] = ev
    return res


def _auditable(ref: dict) -> bool:
    if not ref.get("rule_id"):
        return False
    return ref["decision"] != "allow" or (
        ref["rule_id"] in gc.ESCAPABLE and str(ref.get("reason", "")).startswith("escape"))


# ---------------------------------------------------------------------------
# Running the shims
# ---------------------------------------------------------------------------


def _test_base_dir(tmp_root: Path) -> Path:
    """A temp root that is NOT under */AppData/Local/Temp/* (the guard excludes
    every path there, which would turn the whole sweep into silent allows).
    Short names keep the deepest fixture path under Windows MAX_PATH. The caller
    removes it (see ``guard_root`` and ``_run_chunk``)."""
    if "/appdata/local/temp/" in str(tmp_root).replace("\\", "/").lower() + "/":
        base = Path.home() / ".cache" / "mgt"
        base.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="r", dir=base))
    return tmp_root


@pytest.fixture
def guard_root(tmp_path):
    """Per-test relocation root, removed at teardown when it lives outside tmp_path."""
    root = _test_base_dir(tmp_path)
    yield root
    if root != tmp_path:
        shutil.rmtree(root, ignore_errors=True)


def _pick_env(names: tuple[str, ...]) -> dict:
    """Named variables from this process, once each (os.environ is case-insensitive on Windows)."""
    out: dict[str, str] = {}
    for k in names:
        v = os.environ.get(k)
        if v is not None and k.upper() not in {x.upper() for x in out}:
            out[k] = v
    return out


def _ps_env(extra: dict | None = None) -> dict:
    """Only what powershell.exe needs to start; nothing that the guard reads leaks in."""
    env = _pick_env(("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATH", "PATHEXT", "COMSPEC"))
    env.update(extra or {})
    return env


def _bash_env() -> dict:
    env = _pick_env(("PATH", "SYSTEMROOT"))
    env.setdefault("PATH", "/usr/bin:/bin")
    if BASH and IS_WIN:
        env["PATH"] = str(Path(BASH).parent) + os.pathsep + env["PATH"]
    return env


def _sq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# Host-resource failures that the MSYS2 runtime (Git Bash) or powershell.exe print on
# stderr when the machine is saturated -- the known bash-subprocess flake. The shims
# themselves never write stderr, so any of these means "the host failed", not "the shim
# decided": the call is retried and, if the host never recovers, the test skips.
_HOST_FLAKE_RE = re.compile(
    rb"fork: |Resource temporarily unavailable|cygheap|died waiting for dll loading|fatal error - "
    rb"|couldn't allocate|Insufficient system resources|paging file is too small|Not enough (?:memory|storage)",
    re.I,
)


_DEADLINE = [float("inf")]  # monotonic deadline of the running test (set by ``_deadline``)
_MIN_CALL_S = 3.0


@pytest.fixture(autouse=True)
def _deadline():
    _DEADLINE[0] = time.monotonic() + _TEST_BUDGET_S
    yield
    _DEADLINE[0] = float("inf")


def _time_left() -> float:
    return _DEADLINE[0] - time.monotonic()


def _run(args: list[str], *, stdin: bytes = b"", env: dict, cwd: Path | None = None,
         timeout: float = _RUN_TIMEOUT_S, attempts: int = 3) -> subprocess.CompletedProcess | None:
    """Run with retries on timeouts / Windows crash exits / host-resource stderr;
    None when the host never answered within the test's budget."""
    for _ in range(attempts):
        left = _time_left()
        if left < _MIN_CALL_S:
            return None
        try:
            r = subprocess.run(args, input=stdin, env=env, cwd=str(cwd or _REPO), capture_output=True,
                               timeout=min(timeout, left))
        except subprocess.TimeoutExpired:
            continue
        if r.returncode in _WIN_CRASH_CODES or _HOST_FLAKE_RE.search(r.stderr or b""):
            continue
        return r
    return None


def _run_ps_hook(script: Path, payload_text: str, env: dict) -> subprocess.CompletedProcess | None:
    return _run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                stdin=payload_text.encode("utf-8"), env=_ps_env(env))


def _run_ps_hook_registered(script: Path, payload_text: str, env: dict) -> subprocess.CompletedProcess | None:
    """The form .claude/settings.json registers (``"shell": "powershell"``,
    ``& "$CLAUDE_PROJECT_DIR\\.claude\\hooks\\<script>.ps1"``): a -Command script block
    invoking the file, with the hook payload on the SAME process's stdin."""
    cmd = "& '" + str(script).replace("'", "''") + "'"
    return _run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", cmd],
                stdin=payload_text.encode("utf-8"), env=_ps_env(env))


def _run_sh_hook(script: Path, payload_text: str, env: dict) -> subprocess.CompletedProcess | None:
    exports = "unset HOME; " + "".join(f"export {k}={_sq(v)}; " for k, v in env.items())
    rel = script.relative_to(_REPO).as_posix()
    return _run([BASH, "-c", exports + f"exec bash {rel}"], stdin=payload_text.encode("utf-8"), env=_bash_env())


def _facts(fs_roots: list[Path]) -> str:
    lines = ["C\t" + ("1" if IS_WIN else "0")]
    seen: set[str] = set()
    for root in fs_roots:
        lines.append("R\t" + _np(root))
        for dirpath, _dirs, files in os.walk(root):
            lines.append("K\tdir\t\t" + _np(dirpath))
            for f in files:
                p = os.path.join(dirpath, f)
                lines.append(f"K\tfile\t{int(os.stat(p).st_mtime)}\t{_np(p)}")
        anc = root.parent
        while True:
            a = _np(anc)
            if a not in seen:
                seen.add(a)
                lines.append("K\tdir\t\t" + a)
                g = anc / ".git"
                kind = "dir" if g.is_dir() else ("file" if g.is_file() else "")
                lines.append(f"K\t{kind}\t\t{_np(g)}")
            if anc.parent == anc:
                break
            anc = anc.parent
    return "\n".join(lines) + "\n"


def _collect(mat: dict, io: Path, elapsed: float) -> dict:
    trace_p = io / "trace.json"
    out_p = io / "out.txt"
    return {
        "mat": mat,
        "trace": json.loads(trace_p.read_text(encoding="utf-8")) if trace_p.exists() else None,
        "out": out_p.read_text(encoding="utf-8") if out_p.exists() else "",
        "state_after": mat["state_path"].read_text(encoding="utf-8") if mat["state_path"].exists() else None,
        "audit_after": mat["audit_path"].read_text(encoding="utf-8") if mat["audit_path"].exists() else "",
        "elapsed": elapsed,
        "engine_ms": float((io / "ms.txt").read_text()) if (io / "ms.txt").exists() else None,
    }


def _run_chunk(shim: str, chunk: list[dict], chunk_dir: Path) -> dict[str, dict] | None:
    t0 = time.time()
    mats: list[tuple[dict, Path, dict]] = []
    for i, case in enumerate(chunk):
        io = chunk_dir / "io" / f"c{i:03d}"
        io.mkdir(parents=True)
        mat = _materialize(case, chunk_dir / "f" / f"c{i:03d}", t0)
        mat["ref"] = _reference(mat)
        (io / "payload.json").write_text(mat["payload_text"], encoding="utf-8")
        (io / "env.json").write_text(json.dumps(mat["env"]), encoding="utf-8")
        (io / "mode").write_text(mat["mode"], encoding="utf-8")
        mats.append((case, io, mat))
    start = time.time()
    if shim == "ps1":
        r = _run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(PS1),
                  "-Batch", str(chunk_dir / "io")], env=_ps_env(), timeout=_BATCH_TIMEOUT_S, attempts=1)
    else:
        manifest = chunk_dir / "manifest.txt"
        manifest.write_text("\n".join(_np(io) for _c, io, _m in mats) + "\n", encoding="utf-8")
        facts = chunk_dir / "facts.txt"
        facts.write_text(_facts([chunk_dir / "f" / f"c{i:03d}" for i in range(len(chunk))]), encoding="utf-8")
        rel = SH.relative_to(_REPO).as_posix()
        r = _run([BASH, "-c", f"exec bash {rel} --batch {_sq(_np(manifest))} {_sq(_np(facts))}"],
                 env=_bash_env(), timeout=_BATCH_TIMEOUT_S, attempts=1)
    elapsed = time.time() - start + (start - t0)
    out = None if r is None else {case["name"]: _collect(mat, io, elapsed) for case, io, mat in mats}
    shutil.rmtree(chunk_dir, ignore_errors=True)  # everything needed was read into memory
    return out  # None: the host never answered (timeouts / crash exits) -> retry, then skip


_RESULTS: dict[tuple[str, int], dict[str, dict]] = {}
_BASES: dict[str, Path] = {}
_SWEEP_ROOTS: list[Path] = []


@pytest.fixture(scope="module", autouse=True)
def _remove_sweep_roots():
    """The chunk dirs are already gone (``_run_chunk``); only the empty bases remain."""
    yield
    for p in _SWEEP_ROOTS:
        shutil.rmtree(p, ignore_errors=True)


_CHUNK_OF = {c["name"]: i // _CHUNK for i, c in enumerate(ALL_CASES)}


_CHUNK_TRIES: dict[tuple[str, int], int] = {}
_CHUNK_MAX_TRIES = 3


def _result_for(shim: str, name: str, tmp_path_factory) -> dict | None:
    """The shim's result for one case. Cases run in chunks of ``_CHUNK`` through the
    batch entry (one process per chunk, run lazily on first use so no single test
    carries the whole sweep). A chunk the host never answered (timeouts / crash exits
    under contention, or the test's budget ran out) is retried -- by this test while
    its budget lasts, else by the next test of the same chunk -- at most
    ``_CHUNK_MAX_TRIES`` times in all. None = never answered (the caller skips)."""
    k = _CHUNK_OF[name]
    key = (shim, k)
    if shim not in _BASES:
        _BASES[shim] = _test_base_dir(tmp_path_factory.mktemp(f"guard_{shim}"))
        if _BASES[shim].parent.name == "mgt":
            _SWEEP_ROOTS.append(_BASES[shim])
    chunk = ALL_CASES[k * _CHUNK:(k + 1) * _CHUNK]
    while key not in _RESULTS and _CHUNK_TRIES.get(key, 0) < _CHUNK_MAX_TRIES and _time_left() >= _MIN_CALL_S:
        attempt = _CHUNK_TRIES.get(key, 0)
        _CHUNK_TRIES[key] = attempt + 1
        got = _run_chunk(shim, chunk, _BASES[shim] / f"k{k}a{attempt}")
        if got is not None:
            _RESULTS[key] = got  # the process answered: a missing trace is a real engine failure
    return _RESULTS.get(key, {}).get(name)


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------


def _norm_state_view(st: Any) -> dict:
    return gc._norm_state(st)


def _assert_state_parity(ref_state: dict, got_text: str | None, tol: float) -> None:
    assert got_text is not None, "the core changed the session state but the shim wrote no state file"
    got = _norm_state_view(json.loads(got_text))
    exp = _norm_state_view(ref_state)
    assert got["denies"] == exp["denies"]
    for key in ("code_receipts", "research_receipts"):
        assert len(got[key]) == len(exp[key]), key
        for g, e in zip(got[key], exp[key]):
            assert abs(g[0] - e[0]) <= tol, (key, g, e)
            assert g[1:] == e[1:], (key, g, e)
    assert len(got["capture_receipts"]) == len(exp["capture_receipts"])
    for g, e in zip(got["capture_receipts"], exp["capture_receipts"]):
        assert abs(g - e) <= tol
    for key in ("degraded_until", "web_reminder_at"):
        assert (got[key] == 0) == (exp[key] == 0), key
        assert abs(got[key] - exp[key]) <= tol, key
    assert set(got["advisory_seen"]) == set(exp["advisory_seen"])
    for k, v in exp["advisory_seen"].items():
        assert abs(got["advisory_seen"][k] - v) <= tol, k


def _assert_parity(case: dict, res: dict) -> None:
    mat = res["mat"]
    ref = mat["ref"]
    tr = res["trace"]
    assert tr is not None, "the shim produced no result for this case"
    assert not tr.get("error"), tr.get("error")
    detail = f"shim={tr!r}\ncore={ {k: v for k, v in ref.items() if k != 'state'}!r}"
    if not mat["in_scope"]:
        # the brief shim's events: these shims must stay silent and touch nothing
        assert tr["decision"] == "allow" and res["out"] == "", detail
        assert res["state_after"] == mat["state_before"], detail
        assert res["audit_after"] == mat["audit_before"], detail
        return
    assert tr["decision"] == ref["decision"], detail
    assert tr["rule_id"] == ref["rule_id"], detail
    if ref.get("project") is not None or ref["decision"] != "allow":
        assert tr["project"] == ref.get("project"), detail
    if ref.get("shadowed") is not None:
        assert tr["shadowed"] == ref["shadowed"], detail
    if ref["decision"] != "allow":
        assert tr["reason"] == ref["reason"], detail
    expected_out = gc.render_output(ref["_event"], ref)
    got_out = json.loads(res["out"]) if res["out"].strip() else None
    assert got_out == expected_out, detail
    # the relocated fixture expectation (independent of the core)
    if not case.get("parity_only") and (IS_WIN or case["name"] not in _WINDOWS_ONLY_EXPECTATION):
        assert tr["decision"] == case["expected_decision"], detail
        assert tr["rule_id"] == case["expected_rule"], detail
        if "expected_project" in case:
            assert tr["project"] == case["expected_project"], detail
        for s in mat["contains"]:
            assert s in tr["reason"], f"{s!r} not in {tr['reason']!r}"
        for s in mat["excludes"]:
            assert s not in tr["reason"], f"{s!r} unexpectedly in {tr['reason']!r}"
    # the per-session state file
    tol = res["elapsed"] + 10
    if isinstance(ref.get("state"), dict):
        _assert_state_parity(ref["state"], res["state_after"], tol)
    else:
        assert res["state_after"] == mat["state_before"], "the shim changed the state file; the core did not"
    # the audit line (never command text)
    new_lines = res["audit_after"][len(mat["audit_before"]):].splitlines()
    if _auditable(ref):
        assert len(new_lines) == 1, new_lines
        line = json.loads(new_lines[0])
        assert line["rule"] == ref["rule_id"] and line["decision"] == ref["decision"]
        assert line["event"] == ref["_event"] and line["session"] == mat["sid"]
        assert line["tool"] == (mat["payload"].get("tool_name") if isinstance(mat["payload"], dict) else None)
        assert line["root"] == ref.get("root")
        assert abs(line["ts"] - time.time()) <= tol + 3600
        assert set(line) == {"ts", "event", "rule", "decision", "tool", "root", "session"}
    else:
        assert new_lines == [], new_lines


# ---------------------------------------------------------------------------
# 1. Static checks (no subprocess needed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", SHIM_FILES, ids=lambda p: p.name)
def test_shim_files_exist_and_are_pure_ascii(path):
    raw = path.read_bytes()
    assert raw, path
    bad = [i for i, b in enumerate(raw) if b > 0x7F]
    assert not bad, f"{path.name} has non-ASCII bytes at {bad[:5]} (PS 5.1 reads BOM-less UTF-8 as cp1252)"
    assert b"\r\n" not in raw or path.suffix == ".ps1", f"{path.name} must use LF line endings"


@pytest.mark.parametrize("path", SHIM_FILES, ids=lambda p: p.name)
def test_shims_never_exit_2(path):
    """Exit 2 would block with stderr; the only blocking channel is the JSON decision."""
    text = path.read_text(encoding="ascii")
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    assert not re.search(r"\bexit\s+2\b|\bexit\s*\(\s*2\s*\)", code, re.I), path.name


def _code_only(path: Path) -> str:
    """The file without comment text (a '#' starts a comment in ps1, sh and awk)."""
    return "\n".join(line.split("#", 1)[0] for line in path.read_text(encoding="ascii").splitlines())


_FORBIDDEN_CODE = (
    r"hooks\.(?:ps1|sh)\b", r"meridian\.toml", r"[\\/'\"]\.env\b", r"Invoke-(?:WebRequest|RestMethod)",
    r"\bcurl\b", r"\bwget\b", r"Net\.WebClient", r"HttpClient", r"\bpython[0-9.]*(?:\.exe)?\b", r"\bpy\s+-",
    r"\bsqlite3?\b",
)


@pytest.mark.parametrize("path", SHIM_FILES, ids=lambda p: p.name)
def test_hot_path_has_no_network_python_sqlite_or_credentials(path):
    code = _code_only(path)
    for pat in _FORBIDDEN_CODE:
        assert not re.search(pat, code, re.I), (path.name, pat)


_AUTOLOAD_CMDLETS = (
    "New-Object", "Add-Type", "Join-Path", "Split-Path", "Sort-Object", "Select-Object", "Measure-Object",
    "ConvertTo-Json", "Get-Content", "Set-Content", "Out-File", "Test-Path", "Get-Item", "Get-ChildItem",
    "Invoke-Expression", "Start-Process", "Get-Date",
)


@pytest.mark.parametrize("path", [PS1, PS1_POST], ids=lambda p: p.name)
def test_ps1_hot_path_uses_no_module_autoloading_cmdlets(path):
    """Each hook call is a fresh powershell.exe; auto-loading Microsoft.PowerShell.Utility
    (New-Object, Add-Type, ...) alone cost ~0.3 s per call. Only the PowerShell 7
    fallback may use ConvertFrom-Json (there is no System.Web.Extensions there)."""
    code = _code_only(path)
    # verb names inside string literals (the shell classifier's verb sets) are data, not calls
    code = re.sub(r"'(?:[^'\n]|'')*'", "''", code)
    code = re.sub(r'"(?:[^"\n`]|`.)*"', '""', code)
    for cmdlet in _AUTOLOAD_CMDLETS:
        assert not re.search(r"(?<![\w-])" + re.escape(cmdlet) + r"(?![\w-])", code, re.I), (path.name, cmdlet)
    uses = re.findall(r"ConvertFrom-Json", code)
    assert len(uses) <= 1 and ("-AsHashtable" in code if uses else True)


@pytest.mark.parametrize("path", [SH, SH_POST], ids=lambda p: p.name)
def test_sh_shims_reexec_under_bash_before_any_bash_syntax(path):
    """dash (``sh file``) exits 2 -- "block" -- on ``${BASH_SOURCE[0]}``; the shims must
    hand over to bash (or exit 0 without it) before any bash-only line is parsed."""
    code = [line.strip() for line in _code_only(path).splitlines() if line.strip()]
    assert code[0] == 'if [ -z "${BASH_VERSION:-}" ]; then', code[:3]
    assert code[1:5] == ["command -v bash >/dev/null 2>&1 || exit 0", 'exec bash "$0" "$@"', "exit 0", "fi"]
    before = "\n".join(code[:5])
    assert "BASH_SOURCE" not in before and "$'" not in before and "[[" not in before


def test_post_shims_are_thin_wrappers_over_the_engines():
    assert "meridian_guard.awk" in SH.read_text(encoding="ascii")
    assert "--post" in _code_only(SH_POST)
    assert "-Mode post" in _code_only(PS1_POST)


@needs_ps
@pytest.mark.parametrize("path", [PS1, PS1_POST], ids=lambda p: p.name)
def test_ps1_parses_with_zero_errors(path):
    cmd = ("$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
           f"'{path}', [ref]$null, [ref]$e); $e.Count")
    r = _run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", cmd], env=_ps_env())
    if r is None:
        pytest.skip("host contention")
    assert r.stdout.decode().strip() == "0", r.stdout + r.stderr


@needs_bash
def test_sh_syntax_and_awk_program_load():
    for path in (SH, SH_POST):
        rel = path.relative_to(_REPO).as_posix()
        r = _run([BASH, "-n", rel], env=_bash_env())
        if r is None:
            pytest.skip("host contention")
        assert r.returncode == 0, r.stderr
    rel = AWK.relative_to(_REPO).as_posix()
    # an empty batch manifest makes the engine load, run and exit without any case
    r = _run([BASH, "-c", f"printf '' > /dev/null; MG_BATCH=/dev/null LC_ALL=C awk -f {rel} </dev/null; echo rc=$?"],
             env=_bash_env())
    if r is None:
        pytest.skip("host contention")
    assert r.stdout.decode().strip() == "rc=0" and r.stderr == b"", (r.stdout, r.stderr)


def test_every_fixture_case_is_routed():
    in_scope = [c for c in CASES if _mode_for(c)]
    assert len(in_scope) >= 360, "nearly every fixture case is a PreToolUse/PostToolUse case"
    assert {c["event"] for c in CASES if not _mode_for(c)} <= {"SessionStart", "SubagentStart", "Notification"}


def test_relocation_is_prefix_only_and_complete(tmp_path):
    rl = _Reloc(tmp_path / "x")
    assert rl.s("C:\\Users\\13144\\a") == rl.c_fwd.replace("/", "\\") + "\\a"
    assert rl.s("c:/users/13144/Documents") == rl.c_fwd + "/Documents"
    assert rl.s("D:/nonexistent/x") == rl.d_fwd + "/nonexistent/x"
    assert rl.s("HKCU:\\Environment /d G1") == "HKCU:\\Environment /d G1"
    assert rl.s("C-Users-13144-Documents-Meridian-repository") == "C-Users-13144-Documents-Meridian-repository"
    assert rl.s("ps1 -D:x") == "ps1 -D:x"


# ---------------------------------------------------------------------------
# 2. Parity sweep: every fixture case through each shim
# ---------------------------------------------------------------------------


@needs_ps
@pytest.mark.parametrize("case", ALL_CASES, ids=[c["name"] for c in ALL_CASES])
def test_ps1_matches_guard_core(case, tmp_path_factory):
    res = _result_for("ps1", case["name"], tmp_path_factory)
    if res is None:
        pytest.skip("PowerShell never completed this case's batch on this host (timeouts / crash exits under contention)")
    _assert_parity(case, res)


@needs_bash
@pytest.mark.parametrize("case", ALL_CASES, ids=[c["name"] for c in ALL_CASES])
def test_sh_matches_guard_core(case, tmp_path_factory):
    res = _result_for("sh", case["name"], tmp_path_factory)
    if res is None:
        pytest.skip("bash never completed this case's batch on this host (the known subprocess flake under contention)")
    _assert_parity(case, res)


@pytest.mark.parametrize("shim", ["ps1", "sh"])
def test_batch_entry_refuses_the_live_guard_dir(shim, guard_root):
    """The test-only batch entry must never write receipts/counters into the guard
    state of the environment it runs in (that dir is owner-controlled, G9)."""
    if shim == "ps1" and POWERSHELL is None:
        pytest.skip("PowerShell unavailable")
    if shim == "sh" and BASH is None:
        pytest.skip("bash unavailable")
    base = guard_root
    mat = _materialize(_case("D1_grep_repo_root"), base / "f", time.time())
    io = base / "io" / "c000"
    io.mkdir(parents=True)
    (io / "payload.json").write_text(mat["payload_text"], encoding="utf-8")
    (io / "env.json").write_text(json.dumps(mat["env"]), encoding="utf-8")
    (io / "mode").write_text("pre", encoding="utf-8")
    live = {"LOCALAPPDATA": mat["env"]["LOCALAPPDATA"], "USERPROFILE": mat["env"]["USERPROFILE"]}
    if shim == "ps1":
        r = _run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(PS1),
                  "-Batch", str(base / "io")], env=_ps_env(live))
    else:
        manifest = base / "manifest.txt"
        manifest.write_text(_np(io) + "\n", encoding="utf-8")
        exports = "".join(f"export {k}={_sq(v)}; " for k, v in live.items())
        rel = SH.relative_to(_REPO).as_posix()
        r = _run([BASH, "-c", exports + f"exec bash {rel} --batch {_sq(_np(manifest))}"], env=_bash_env())
    if r is None:
        pytest.skip("host contention")
    trace = json.loads((io / "trace.json").read_text(encoding="utf-8"))
    assert trace["error"] == "batch refuses the live guard dir" and trace["decision"] == "allow"
    assert not mat["state_path"].exists(), "no state was written into the live guard dir"
    assert mat["audit_path"].read_text(encoding="utf-8") == mat["audit_before"]


# ---------------------------------------------------------------------------
# 3. Real hook invocations (stdin -> stdout, exactly as Claude Code runs them)
# ---------------------------------------------------------------------------


def _case(name: str) -> dict:
    return next(c for c in CASES if c["name"] == name)


def _real(shim: str, mat: dict, payload_text: str | None = None) -> subprocess.CompletedProcess:
    text = mat["payload_text"] if payload_text is None else payload_text
    if shim == "ps1":
        script = PS1 if mat["mode"] == "pre" else PS1_POST
        r = _run_ps_hook(script, text, mat["env"])
    else:
        script = SH if mat["mode"] == "pre" else SH_POST
        r = _run_sh_hook(script, text, mat["env"])
    if r is None:
        pytest.skip(f"{shim}: the host never completed the hook process (timeouts / crash exits under contention)")
    return r


def _assert_real_matches(r: subprocess.CompletedProcess, mat: dict) -> None:
    ref = mat["ref"]
    assert r.returncode == 0, (r.returncode, r.stderr[-2000:])
    out = r.stdout.decode("utf-8", "replace")
    got = json.loads(out) if out.strip() else None
    assert got == gc.render_output(ref["_event"], ref), (out, ref)
    case = mat["case"]
    if mat["in_scope"] and (IS_WIN or case["name"] not in _WINDOWS_ONLY_EXPECTATION):
        if case["expected_decision"] == "allow":
            assert got is None
        elif case["expected_decision"] in ("deny", "ask"):
            assert got["hookSpecificOutput"]["permissionDecision"] == case["expected_decision"]
            reason = got["hookSpecificOutput"]["permissionDecisionReason"]
            for s in mat["contains"]:
                assert s in reason
            for s in mat["excludes"]:
                assert s not in reason
        else:
            assert "additionalContext" in got["hookSpecificOutput"]


_REAL_CASES = _REPLAY + [
    "G1_missing_snapshot_allows", "G1_corrupt_snapshot_allows", "G10_edit_removes_guard_entry",
    "G13_second_error_degraded", "G14_no_confirmation_directive", "G2_stale_index_advisory",
]


@needs_ps
@pytest.mark.parametrize("name", _REAL_CASES)
def test_ps1_real_invocation(name, guard_root):
    base = guard_root
    mat = _materialize(_case(name), base / "f", time.time())
    mat["ref"] = _reference(mat)
    r = _real("ps1", mat)
    _assert_real_matches(r, mat)


# On Windows every Git Bash process spawn goes through MSYS fork emulation, so one real
# sh hook call costs 5-13 s under load (it is the POSIX hosts' shim; Windows hosts
# register the ps1). There the one-call-per-case check runs a representative subset --
# one case per rule family plus the fail-safe snapshot cases -- because the batch sweep
# above already runs the sh shim on EVERY fixture case. POSIX hosts run all of them.
_SH_REAL_CASES = _REAL_CASES if not IS_WIN else [
    "D1_grep_repo_root", "D3_grep_dnabert", "D4_write_automem", "D6_bash_grep_rn", "D8_cbm_stale_duplicate",
    "A2_git_log_grep", "A7_read_worktree_file", "G1_missing_snapshot_allows", "G1_corrupt_snapshot_allows",
    "G14_no_confirmation_directive",
]


@needs_bash
@pytest.mark.parametrize("name", _SH_REAL_CASES)
def test_sh_real_invocation(name, guard_root):
    base = guard_root
    mat = _materialize(_case(name), base / "f", time.time())
    mat["ref"] = _reference(mat)
    r = _real("sh", mat)
    _assert_real_matches(r, mat)


_REGISTERED_FORM_CASES = [
    "D1_grep_repo_root", "D4_write_automem", "D7_ps_gci_recurse_sls", "A1_grep_memory_dir_read",
    "G13_code_intel_ok_receipt", "G14_no_confirmation_directive",
]


@needs_ps
@pytest.mark.parametrize("name", _REGISTERED_FORM_CASES)
def test_ps1_registered_command_form(name, guard_root):
    """The exact form .claude/settings.json uses (``& "<path>"`` under ``shell:
    powershell``) reads the payload from stdin and matches the core: stdout, state
    file, and nothing on stderr."""
    mat = _materialize(_case(name), guard_root / "f", time.time())
    mat["ref"] = _reference(mat)
    t0 = time.time()
    r = _run_ps_hook_registered(PS1 if mat["mode"] == "pre" else PS1_POST, mat["payload_text"], mat["env"])
    if r is None:
        pytest.skip("PowerShell never completed the hook process (timeouts / crash exits under contention)")
    _assert_real_matches(r, mat)
    assert r.stderr == b"", r.stderr[-2000:]
    after = mat["state_path"].read_text(encoding="utf-8") if mat["state_path"].exists() else None
    if isinstance(mat["ref"].get("state"), dict):
        _assert_state_parity(mat["ref"]["state"], after, time.time() - t0 + 10)
    else:
        assert after == mat["state_before"]


# ---------------------------------------------------------------------------
# 4. Fail-safe: bad input never blocks
# ---------------------------------------------------------------------------

_BAD_STDIN = {
    "empty": "",
    "whitespace": "  \n\t ",
    "garbage": "this is not json {",
    "array": '["not", "an", "object"]',
    "string": '"garbage"',
    "truncated": '{"tool_name": "Grep", "tool_input": {"pattern": "x", "path": "C:/Users/13144/Docu',
    "nul_ish": '{"tool_name": "Grep", "tool_input": 5}',
}


@pytest.mark.parametrize("shim", ["ps1", "sh"])
@pytest.mark.parametrize("label", list(_BAD_STDIN))
def test_bad_stdin_allows(shim, label, guard_root):
    if shim == "ps1" and POWERSHELL is None:
        pytest.skip("PowerShell unavailable")
    if shim == "sh" and BASH is None:
        pytest.skip("bash unavailable")
    base = guard_root
    mat = _materialize(_case("D1_grep_repo_root"), base / "f", time.time())
    r = _real(shim, mat, payload_text=_BAD_STDIN[label])
    assert r.returncode == 0
    assert r.stdout.strip() == b"", r.stdout


@pytest.mark.parametrize("shim", ["ps1", "sh"])
def test_unreadable_state_file_is_treated_as_empty(shim, guard_root):
    """A state path that cannot be read (here: a directory) behaves like an empty
    state -- no crash, no spurious block: an allowed call stays allowed and a
    code-search call gets the core's (empty-state) decision, failed open because
    the state cannot be saved either (fix round 1: without saved state the breaker
    and the receipt escape cannot work, so an escapable deny becomes an inject)."""
    if shim == "ps1" and POWERSHELL is None:
        pytest.skip("PowerShell unavailable")
    if shim == "sh" and BASH is None:
        pytest.skip("bash unavailable")
    base = guard_root
    for name in ("A8_grep_unindexed_repo", "D1_grep_repo_root"):
        mat = _materialize(_case(name), base / name, time.time())
        mat["state_path"].parent.mkdir(parents=True, exist_ok=True)
        mat["state_path"].mkdir()
        mat["ref"] = gc.evaluate("PreToolUse", mat["payload"], mat["snapshot"], None, mat["env"], now=time.time())
        mat["ref"]["_event"] = "PreToolUse"
        r = _real(shim, mat)
        assert r.returncode == 0
        out = r.stdout.decode("utf-8", "replace")
        got = json.loads(out) if out.strip() else None
        want = gc.fail_open_result(mat["ref"]) if isinstance(mat["ref"].get("state"), dict) else mat["ref"]
        assert got == gc.render_output("PreToolUse", want), (name, out)
        assert mat["state_path"].is_dir(), "the unreadable state path is left alone"


@pytest.mark.parametrize("shim", ["ps1", "sh"])
def test_missing_engine_file_allows(shim, tmp_path):
    """A shim copied without its engine (or with a broken one) fails open."""
    if shim == "ps1" and POWERSHELL is None:
        pytest.skip("PowerShell unavailable")
    if shim == "sh" and BASH is None:
        pytest.skip("bash unavailable")
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Write",
                          "tool_input": {"file_path": "~/.claude/projects/x/memory/a.md", "content": "x"}})
    if shim == "sh":
        lone = tmp_path / "hooks"
        lone.mkdir()
        shutil.copy(SH, lone / "meridian_guard.sh")
        rel = os.path.relpath(lone / "meridian_guard.sh", _REPO).replace("\\", "/")
        r = _run([BASH, "-c", f"exec bash {_sq(rel)}"], stdin=payload.encode(), env=_bash_env())
    else:
        lone = tmp_path / "hooks"
        lone.mkdir()
        shutil.copy(PS1_POST, lone / "meridian_guard_post.ps1")
        r = _run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                  str(lone / "meridian_guard_post.ps1")], stdin=payload.encode(), env=_ps_env())
    if r is None:
        pytest.skip("host contention")
    assert r.returncode == 0 and r.stdout.strip() == b""


_MEMORY_WRITE = json.dumps({"session_id": "broken", "hook_event_name": "PreToolUse", "tool_name": "Write",
                            "tool_input": {"file_path": "C:/Users/13144/.claude/projects/p/memory/a.md", "content": "x"},
                            "cwd": "C:/"})


@pytest.mark.parametrize("shim", ["ps1", "sh"])
def test_broken_engine_fails_open(shim, tmp_path):
    """An engine that fails while LOADING -- a terminating error in the ps1's constant
    set-up (which runs before its main try block), or an awk program that does not
    parse -- exits 0 with no stdout and no stderr: never a block, never exit 2. The
    payload is a G6 memory write, which a working engine denies."""
    if shim == "ps1" and POWERSHELL is None:
        pytest.skip("PowerShell unavailable")
    if shim == "sh" and BASH is None:
        pytest.skip("bash unavailable")
    lone = tmp_path / "hooks"
    lone.mkdir()
    env = {"USERPROFILE": "C:\\Users\\13144", "LOCALAPPDATA": str(tmp_path / "lad")}
    if shim == "ps1":
        lines = PS1.read_text(encoding="ascii").split("\n")
        anchor = next(i for i, line in enumerate(lines) if line.startswith("$script:G13_DEGRADED_MSG = "))
        lines.insert(anchor + 1, "[void][System.Text.UTF8Encoding]::NoSuchMethodDuringInit()")
        (lone / "meridian_guard.ps1").write_text("\n".join(lines), encoding="ascii", newline="")
        shutil.copy(PS1_POST, lone / "meridian_guard_post.ps1")
        runs = [_run_ps_hook(lone / "meridian_guard.ps1", _MEMORY_WRITE, env),
                _run_ps_hook_registered(lone / "meridian_guard.ps1", _MEMORY_WRITE, env),
                _run_ps_hook(lone / "meridian_guard_post.ps1", _MEMORY_WRITE.replace("PreToolUse", "PostToolUse"), env)]
    else:
        shutil.copy(SH, lone / "meridian_guard.sh")
        shutil.copy(SH_POST, lone / "meridian_guard_post.sh")
        (lone / "meridian_guard.awk").write_text("BEGIN { this is not awk (\n", encoding="ascii", newline="")
        exports = "".join(f"export {k}={_sq(v)}; " for k, v in env.items())
        runs = []
        for name in ("meridian_guard.sh", "meridian_guard_post.sh"):
            rel = os.path.relpath(lone / name, _REPO).replace("\\", "/")
            runs.append(_run([BASH, "-c", exports + f"exec bash {_sq(rel)}"], stdin=_MEMORY_WRITE.encode(), env=_bash_env()))
    if any(r is None for r in runs):
        pytest.skip("host contention")
    for r in runs:
        assert (r.returncode, r.stdout, r.stderr) == (0, b"", b""), (r.returncode, r.stdout[-500:], r.stderr[-500:])
    # the control: the intact engine denies the very same payload
    if shim == "ps1":
        ok = _run_ps_hook(PS1, _MEMORY_WRITE, env)
    else:
        ok = _run_sh_hook(SH, _MEMORY_WRITE, env)
    if ok is None:
        pytest.skip("host contention")
    assert json.loads(ok.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------------------
# 5. Sequences through the real hooks (state carried in the state file)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shim", ["ps1", "sh"])
def test_kill_switch_sentinel_flips_the_next_call(shim, guard_root):
    if shim == "ps1" and POWERSHELL is None:
        pytest.skip("PowerShell unavailable")
    if shim == "sh" and BASH is None:
        pytest.skip("bash unavailable")
    base = guard_root
    mat = _materialize(_case("D1_grep_repo_root"), base / "f", time.time())
    mat["ref"] = _reference(mat)
    gdir = Path(mat["gdir"])

    def decision() -> str | None:
        r = _real(shim, mat)
        out = r.stdout.decode("utf-8", "replace").strip()
        if not out:
            return None
        hso = json.loads(out)["hookSpecificOutput"]
        return hso.get("permissionDecision") or ("inject" if "additionalContext" in hso else "?")

    assert decision() == "deny"
    (gdir / "guard.off").write_text("")
    assert decision() is None, "guard.off created mid-run: the next call is allowed with no output"
    (gdir / "guard.off").unlink()
    (gdir / "guard.advisory").write_text("")
    assert decision() == "inject", "guard.advisory turns the deny into additionalContext"
    (gdir / "guard.advisory").unlink()


@pytest.mark.parametrize("shim", ["ps1", "sh"])
def test_deny_then_receipt_then_retry_is_allowed(shim, guard_root):
    """PreToolUse deny -> PostToolUse code-intel receipt (post shim) -> the retry escapes.
    Proves the pre and post shims share the state file format."""
    if shim == "ps1" and POWERSHELL is None:
        pytest.skip("PowerShell unavailable")
    if shim == "sh" and BASH is None:
        pytest.skip("bash unavailable")
    base = guard_root
    grep = _materialize(_case("D1_grep_repo_root"), base / "f", time.time())
    grep["ref"] = _reference(grep)
    r1 = _real(shim, grep)
    assert json.loads(r1.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    st = json.loads(grep["state_path"].read_text(encoding="utf-8"))
    assert st["denies"] == 1
    slug = "C-Users-13144-Documents-Meridian-repository"
    post_payload = {"session_id": "replay-session", "hook_event_name": "PostToolUse",
                    "tool_name": "mcp__codebase-memory__search_code", "tool_input": {"project": slug, "pattern": "arxiv"},
                    "tool_response": "{\"results\": []}", "cwd": grep["payload"]["cwd"]}
    post = dict(grep, mode="post", payload=post_payload, payload_text=json.dumps(post_payload))
    r2 = _real(shim, post)
    assert r2.returncode == 0 and r2.stdout.strip() == b""
    st = json.loads(grep["state_path"].read_text(encoding="utf-8"))
    assert len(st["code_receipts"]) == 1 and st["code_receipts"][0][1] is True and st["code_receipts"][0][2] == slug
    assert st["denies"] == 1
    r3 = _real(shim, grep)
    assert r3.returncode == 0 and r3.stdout.strip() == b"", "the retry after a code-intel receipt is allowed"
    audit = [json.loads(x) for x in grep["audit_path"].read_text(encoding="utf-8").splitlines()]
    assert [(a["rule"], a["decision"]) for a in audit] == [("G1", "deny"), ("G1", "allow")]
    # the core reads the shim-written state and agrees
    again = gc.evaluate("PreToolUse", grep["payload"], grep["snapshot"], st, grep["env"], now=time.time())
    assert again["decision"] == "allow" and again["reason"].startswith("escape")


# ---------------------------------------------------------------------------
# 6. Latency (reported; asserted only against gross regressions)
# ---------------------------------------------------------------------------


def _pct(values: list[float], q: float) -> float:
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return s[k]


@pytest.mark.parametrize("shim", ["ps1", "sh"])
def test_latency_report(shim, guard_root, record_property, capsys):
    """p50/p95 wall time of one real hook invocation on this host.

    ``baseline`` is the bare interpreter start (``powershell -Command exit`` /
    ``bash -c true``); the guard's own cost is the difference. Only a gross
    regression fails (engine overhead above 4 s at p50 for PowerShell -- the
    module auto-load mistake this guards against cost ~0.3 s per cmdlet module,
    and heavy host contention roughly doubles everything -- or any call past 30 s).
    At least 2 rounds run, then the loop stops after ~30 s so the test stays inside
    the suite's 60 s per-test timeout even on a saturated host.
    Absolute numbers depend on host load, and a hook that overruns its timeout
    fails OPEN by design.
    """
    if shim == "ps1" and POWERSHELL is None:
        pytest.skip("PowerShell unavailable")
    if shim == "sh" and BASH is None:
        pytest.skip("bash unavailable")
    base = guard_root
    n = int(os.environ.get("MERIDIAN_GUARD_LATENCY_N", "6"))
    deny = _materialize(_case("D1_grep_repo_root"), base / "d", time.time())
    fast = _materialize(_case("A7_read_worktree_file"), base / "a", time.time())
    if shim == "ps1":
        base_args = [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", "exit 0"]
        benv = _ps_env()
    else:
        base_args = [BASH, "-c", "true"]
        benv = _bash_env()
    timings: dict[str, list[float]] = {"baseline": [], "deny_D1": [], "read_A7": []}
    started = time.perf_counter()
    for i in range(n):
        # stay inside the suite's per-test timeout on a contended host: >= 2 rounds, then a budget
        if i >= 2 and time.perf_counter() - started > 30:
            break
        t = time.perf_counter()
        if _run(base_args, env=benv) is None:
            pytest.skip("host contention")
        timings["baseline"].append(time.perf_counter() - t)
        for key, mat in (("deny_D1", deny), ("read_A7", fast)):
            t = time.perf_counter()
            _real(shim, mat)
            timings[key].append(time.perf_counter() - t)
    report = {k: {"p50_s": round(statistics.median(v), 3), "p95_s": round(_pct(v, 0.95), 3), "n": len(v)}
              for k, v in timings.items()}
    record_property(f"{shim}_latency", json.dumps(report))
    with capsys.disabled():
        print(f"\n[guard latency {shim}] {json.dumps(report)}")
    assert max(max(v) for v in timings.values()) < 30
    if shim == "ps1":
        overhead = report["deny_D1"]["p50_s"] - report["baseline"]["p50_s"]
        assert overhead < 4.0, report
