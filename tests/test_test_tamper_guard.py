"""43539c70 - the PostToolUse test-tamper guard.

Named failure mode the other hooks DON'T catch: an agent makes a FAILING test pass
by editing the TEST (loosening an assertion / deleting a case) instead of fixing the
code under test. This guard FLAGS a test-file edit so the human/agent is reminded to
confirm the change fixes the code, not the assertion. It is complementary to the Stop
hook (sprint_guard.*) and the PreToolUse guards (hitl_guard.*).

Two layers are tested:

1. The hook itself (``test_tamper_guard.sh`` via bash) - the primary deliverable.
   Default posture is NON-BLOCKING: it warns on stderr but exits 0. Only when
   MERIDIAN_TEST_TAMPER_BLOCK=1 is set does a flagged edit exit 2. A non-test edit
   passes clean; malformed/garbage stdin fails OPEN (exit 0). settings.json really
   wires it as a PostToolUse hook.

2. The server-side exemption signal (``sprint_test_coverage_expected``) that lets the
   hook stay silent when the in-progress sprint item explicitly calls for tests.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from meridian import db as db_module

_REPO = Path(__file__).resolve().parent.parent
_HOOK_SH = _REPO / ".claude" / "hooks" / "test_tamper_guard.sh"
_HOOK_PS1 = _REPO / ".claude" / "hooks" / "test_tamper_guard.ps1"
_SETTINGS = _REPO / ".claude" / "settings.json"

_needs_bash = pytest.mark.skipif(
    not _HOOK_SH.exists() or shutil.which("bash") is None,
    reason="test_tamper_guard.sh or bash unavailable",
)


# Windows NTSTATUS crash exit codes seen when a git-bash subprocess is launched
# under heavy xdist (-n auto) contention: the process is torn down before it can
# run, which is a harness/OS artifact, not a hook result. Retry those.
_WIN_CRASH_CODES = frozenset(
    {
        0xC0000005 & 0xFFFFFFFF,  # ACCESS_VIOLATION
        0xC000007B & 0xFFFFFFFF,  # INVALID_IMAGE_FORMAT
        0xC0000135 & 0xFFFFFFFF,  # DLL_NOT_FOUND
        0xC0000142 & 0xFFFFFFFF,  # DLL_INIT_FAILED (3221225794)
        0xC000013A & 0xFFFFFFFF,  # CONTROL_C_EXIT / kill
        3221225773,              # observed under -n auto (STATUS_* teardown)
    }
)


def _run_hook_once(payload: str, *, block: bool) -> subprocess.CompletedProcess:
    # Relative path + cwd=repo root: an absolute Windows path breaks git-bash's
    # /usr/bin/bash (it wants /c/... MSYS form). Mirrors test_hitl_guard.py.
    #
    # Env is exported INSIDE the bash command rather than via subprocess(env=...):
    # on Windows, git-bash's msys layer drops env keys that weren't already in the
    # native process environment, so a fresh MERIDIAN_TEST_TAMPER_BLOCK passed
    # through subprocess env= never reaches the script. `export` in the shell is
    # portable (Linux CI + Windows) and delivery-independent. MERIDIAN_URL points
    # at an unreachable port so the exemption endpoint fails fast (curl --max-time)
    # and the hook's own flag/exempt logic is what's under test.
    setup = 'export MERIDIAN_URL="http://127.0.0.1:9"; '
    if block:
        setup += 'export MERIDIAN_TEST_TAMPER_BLOCK=1; '
    else:
        setup += 'unset MERIDIAN_TEST_TAMPER_BLOCK; '
    cmd = setup + 'exec bash .claude/hooks/test_tamper_guard.sh'
    # Bytes stdin + defensive decode: under -n auto on Windows the text-mode pipe
    # occasionally cross-wired stdin (garbled UTF-16), a harness seam, not the hook.
    r = subprocess.run(
        ["bash", "-c", cmd],
        input=payload.encode("utf-8"),
        cwd=str(_REPO),
        capture_output=True,
        timeout=90,
    )
    return subprocess.CompletedProcess(
        r.args,
        r.returncode,
        stdout=(r.stdout or b"").decode("utf-8", "replace"),
        stderr=(r.stderr or b"").decode("utf-8", "replace"),
    )


def _run_hook(payload: str, *, block: bool = False) -> subprocess.CompletedProcess:
    # Retry a couple of times on a Windows subprocess-teardown crash (an -n auto
    # contention artifact) or a spawn timeout — never on a real hook exit (0/2).
    last: subprocess.CompletedProcess | None = None
    for _ in range(3):
        try:
            last = _run_hook_once(payload, block=block)
        except subprocess.TimeoutExpired:
            continue
        if last.returncode in _WIN_CRASH_CODES:
            continue
        return last
    assert last is not None, "git-bash never produced a result (all attempts crashed)"
    return last


def _edit(path: str) -> str:
    return json.dumps({"tool_name": "Edit", "tool_input": {"file_path": path}})


# --------------------------------------------------------------------------- #
# The hook - flag-by-default posture
# --------------------------------------------------------------------------- #

_TEST_PATHS = [
    "tests/test_core.py",
    "tests/integration/test_flow.py",
    "meridian/foo/test_bar.py",
    "meridian/foo/bar_test.py",
    "meridian/static/dashboard.test.ts",
    "src/widget.spec.js",
    r"C:\Users\x\repo\tests\test_win.py",  # windows-style separators
    "pkg/__tests__/thing.test.tsx",
]


@_needs_bash
@pytest.mark.parametrize("path", _TEST_PATHS)
def test_flags_test_file_edit_but_does_not_block(path):
    r = _run_hook(_edit(path))
    assert r.returncode == 0, "default posture is NON-BLOCKING (exit 0)"
    assert "test-tamper" in r.stderr.lower(), "must warn about the tamper anti-pattern"
    assert "43539c70" in r.stderr, "must cite the item id"


@_needs_bash
@pytest.mark.parametrize("path", _TEST_PATHS)
def test_block_mode_exits_2_for_test_file(path):
    r = _run_hook(_edit(path), block=True)
    assert r.returncode == 2, "MERIDIAN_TEST_TAMPER_BLOCK=1 => hard block (exit 2)"
    assert r.stderr.strip(), "block must surface a stderr reason"


_NON_TEST_PATHS = [
    "meridian/db/__init__.py",
    "meridian/server.py",
    "meridian/static/dashboard.ts",
    "README.md",
    "contested.py",  # contains 'test' as a substring but is NOT a test file
    "latest_run.py",
    "src/attestation.ts",
]


@_needs_bash
@pytest.mark.parametrize("path", _NON_TEST_PATHS)
def test_non_test_edit_passes_clean(path):
    r = _run_hook(_edit(path))
    assert r.returncode == 0, f"{path} is not a test file - must not flag"
    assert r.stderr.strip() == "", f"{path} must produce no warning"


@_needs_bash
@pytest.mark.parametrize("path", _NON_TEST_PATHS)
def test_non_test_edit_passes_clean_even_in_block_mode(path):
    r = _run_hook(_edit(path), block=True)
    assert r.returncode == 0, f"{path} must never be blocked (it's not a test)"


@_needs_bash
@pytest.mark.parametrize(
    "tool", ["Read", "Bash", "Grep", "Glob", "request_hitl", "WebFetch"]
)
def test_non_writing_tools_ignored(tool):
    payload = json.dumps({"tool_name": tool, "tool_input": {"file_path": "tests/test_core.py"}})
    r = _run_hook(payload)
    assert r.returncode == 0
    assert r.stderr.strip() == "", "only Edit/Write-family tools are inspected"


@_needs_bash
@pytest.mark.parametrize(
    "payload", ["", "not json at all", "{}", '{"foo":"bar"}', '{"tool_name":"Edit"}']
)
def test_fails_open_on_unparseable(payload):
    # Never trap the executor: garbage / missing fields => exit 0, no matter the mode.
    assert _run_hook(payload).returncode == 0
    assert _run_hook(payload, block=True).returncode == 0


# --------------------------------------------------------------------------- #
# settings.json really wires it (present != wired)
# --------------------------------------------------------------------------- #


def test_settings_wires_the_guard_as_posttooluse():
    cfg = json.loads(_SETTINGS.read_text(encoding="utf-8"))
    post = cfg.get("hooks", {}).get("PostToolUse", [])
    entry = next(
        (e for e in post if "test_tamper_guard" in json.dumps(e.get("hooks", []))),
        None,
    )
    assert entry is not None, "PostToolUse must run test_tamper_guard"
    matcher = entry.get("matcher", "")
    assert "Edit" in matcher and "Write" in matcher, "must match file-writing tools"


def test_settings_does_not_disturb_existing_hooks():
    """Adding our entry must not remove the pre-existing guards."""
    cfg = json.loads(_SETTINGS.read_text(encoding="utf-8"))
    hooks = cfg.get("hooks", {})
    pre = json.dumps(hooks.get("PreToolUse", []))
    assert "hitl_guard" in pre, "the HITL PreToolUse guard must still be wired"
    stop = json.dumps(hooks.get("Stop", []))
    assert "sprint_guard" in stop, "the Stop sprint guard must still be wired"


def test_both_hook_scripts_exist():
    assert _HOOK_SH.exists(), "bash hook must ship"
    assert _HOOK_PS1.exists(), "powershell hook must ship"


def test_ps1_is_pure_ascii():
    """PS 5.1 reads BOM-less UTF-8 as cp1252; any non-ASCII byte corrupts it."""
    raw = _HOOK_PS1.read_bytes()
    assert all(b < 128 for b in raw), "test_tamper_guard.ps1 must be pure ASCII"


# --------------------------------------------------------------------------- #
# Server-side exemption signal
# --------------------------------------------------------------------------- #


def test_text_calls_for_test_coverage_keyword_matching():
    f = db_module._text_calls_for_test_coverage
    # Positive: explicit test/coverage intent.
    assert f("Add a test for the OAuth redirect")
    assert f("Improve coverage of the parser")
    assert f("Write regression tests for the outage")
    assert f("Backfill unit test for db helper")
    assert f("TESTS: cover the new endpoint")  # case-insensitive
    # Negative: no test/coverage intent, incl. tricky substrings.
    assert not f("Fix the latest OAuth redirect bug")
    assert not f("Refactor the contested merge logic")
    assert not f("Add attestation to the payload")
    assert not f(None)
    assert not f("")


@pytest.mark.asyncio
async def test_sprint_test_coverage_expected_reflects_in_progress_item(db):
    project = await db_module.create_project(db, "tamper-proj")
    pid = project["id"]

    # No in-progress item -> not expected.
    assert await db_module.sprint_test_coverage_expected(db, pid) is False

    # An in-progress item that does NOT mention tests -> still not expected.
    plain = await db_module.add_sprint_item(db, pid, "v1", "Fix OAuth redirect bug")
    await db_module.claim_sprint_item(db, pid, plain["id"])
    assert await db_module.sprint_test_coverage_expected(db, pid) is False

    # An in-progress item that DOES call for tests -> expected (exemption on).
    covered = await db_module.add_sprint_item(
        db, pid, "v1", "Add a test for the export route"
    )
    await db_module.claim_sprint_item(db, pid, covered["id"])
    assert await db_module.sprint_test_coverage_expected(db, pid) is True


@pytest.mark.asyncio
async def test_sprint_test_coverage_expected_matches_on_notes(db):
    project = await db_module.create_project(db, "tamper-notes")
    pid = project["id"]
    item = await db_module.add_sprint_item(db, pid, "v1", "Ship the widget")
    await db_module.claim_sprint_item(db, pid, item["id"])
    # Title has no test intent -> not expected yet.
    assert await db_module.sprint_test_coverage_expected(db, pid) is False
    # Notes call for coverage -> expected.
    await db_module.patch_sprint_item(
        db, pid, item["id"], notes="Must add regression coverage for the bug"
    )
    assert await db_module.sprint_test_coverage_expected(db, pid) is True


def test_endpoint_returns_flag(client):
    r = client.post("/projects", json={"name": "tamper-ep"})
    assert r.status_code in (200, 201), r.text
    pid = r.json()["id"]

    # No in-progress item -> False.
    r = client.get(f"/projects/{pid}/sprint/test_coverage_expected")
    assert r.status_code == 200
    assert r.json() == {"test_coverage_expected": False}

    # Add + claim a test-focused item -> True.
    r = client.post(
        f"/projects/{pid}/sprint-items",
        json={"version": "v1", "title": "Add unit tests for the parser"},
    )
    assert r.status_code == 201, r.text
    item_id = r.json()["id"]

    # Move it to in_progress via the app's live DB connection (client.app.state.db),
    # on a throwaway loop - the same pattern other client tests use to touch the DB.
    import asyncio  # noqa: PLC0415

    async def _claim() -> None:
        await db_module.claim_sprint_item(client.app.state.db, pid, item_id)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_claim())
    finally:
        loop.close()

    r = client.get(f"/projects/{pid}/sprint/test_coverage_expected")
    assert r.status_code == 200
    assert r.json() == {"test_coverage_expected": True}


def test_endpoint_404_for_unknown_project(client):
    r = client.get("/projects/does-not-exist/sprint/test_coverage_expected")
    assert r.status_code == 404
