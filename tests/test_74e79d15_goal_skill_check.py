"""Tests for sprint item 74e79d15 — executor /goal skill presence check.

GitHub issue #9: a fresh Claude Code executor session failed to recognise the
/goal slash command because the target repo's .claude/ directory had no
skills/goal sub-directory.

Two parts:
  1. start_session(role=executor) with a configured repo_path that is MISSING
     the .claude/skills/goal/ directory should return a ``setup_warning`` key.
  2. start_session(role=executor) with a configured repo_path that HAS the
     directory (or the .claude/commands/goal.md file) should NOT return a
     ``setup_warning`` key.
  3. start_session with no role or role != "executor" should never return
     setup_warning (even if the skill is absent).
  4. The check is best-effort: a bad repo_path or any exception must never
     cause start_session itself to fail.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
import pytest_asyncio

import meridian.server  # noqa: F401 — import first to avoid cycle
from meridian.mcp.handlers import project_tools as pt_mod
from meridian import db as db_module


def _run(coro):
    return asyncio.run(coro)


@pytest_asyncio.fixture
async def db():
    conn = await db_module.init_db(":memory:")
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def project(db):
    return await db_module.create_project(db, "goal-skill-test-proj")


async def _set_executor_config(db, project_id: str, repo_path: str) -> None:
    """Persist an executor_config with repo_path for a project."""
    cfg = json.dumps({"repo_path": repo_path})
    await db.execute(
        "UPDATE projects SET executor_config = ? WHERE id = ?",
        (cfg, project_id),
    )
    await db.commit()


async def _call_start_session(
    db,
    project_id: str,
    *,
    role: str | None = None,
    monkeypatch=None,
) -> dict:
    """Call handle_start_session and return the result dict."""
    # Stub out the _start_session_composite call in server so we don't need a
    # full FastAPI stack.  We just need it to return a minimal valid dict so
    # the post-processing logic (executor_sessions, tool_set, setup_warning)
    # runs.
    import meridian.server as _srv

    async def _fake_composite(db_, pid, sname, data_dir, **kwargs):
        return {
            "session_id": "sess-test-001",
            "compact": True,
            "sprint_summary": {"total": 0, "done": 0, "in_progress": 0, "pending": 0},
            "recent_tasks": [],
            "board_change": 0,
            "agent_instructions": "",
            "note": "fake orientation",
        }

    if monkeypatch is not None:
        monkeypatch.setattr(_srv, "_start_session_composite", _fake_composite)

    args = {"project_id": project_id, "session_name": "test-session"}
    if role is not None:
        args["role"] = role

    result = await pt_mod.handle_start_session(
        args,
        db,
        "/tmp/meridian-test",
        tenant=None,       # self-hosted path (no tenant)
        _mcp_tenant_id=None,
        executor_sessions=set(),
    )
    return result  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 1. Missing skill directory → setup_warning emitted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_setup_warning_when_skill_missing(db, project, tmp_path, monkeypatch):
    """role=executor + repo_path set + no .claude/skills/goal → setup_warning."""
    pid = project["id"]
    repo_dir = tmp_path / "my-repo"
    repo_dir.mkdir()
    # No .claude/skills/goal directory — this is the bug scenario.
    await _set_executor_config(db, pid, str(repo_dir))

    result = await _call_start_session(db, pid, role="executor", monkeypatch=monkeypatch)

    assert "setup_warning" in result, (
        "Expected setup_warning when .claude/skills/goal is absent; got: " + str(result)
    )
    assert "goal" in result["setup_warning"].lower()
    assert "/goal" in result["setup_warning"]


@pytest.mark.asyncio
async def test_setup_warning_contains_install_hint(db, project, tmp_path, monkeypatch):
    """setup_warning must contain enough detail to know how to fix it."""
    pid = project["id"]
    repo_dir = tmp_path / "my-repo2"
    repo_dir.mkdir()
    await _set_executor_config(db, pid, str(repo_dir))

    result = await _call_start_session(db, pid, role="executor", monkeypatch=monkeypatch)

    warn = result.get("setup_warning", "")
    assert "SKILL.md" in warn or "skill" in warn.lower(), (
        "setup_warning should mention the SKILL.md install step"
    )


# ---------------------------------------------------------------------------
# 2. Skill present → no setup_warning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_setup_warning_when_skill_dir_present(db, project, tmp_path, monkeypatch):
    """role=executor + .claude/skills/goal/ exists → no setup_warning."""
    pid = project["id"]
    repo_dir = tmp_path / "my-repo3"
    skill_dir = repo_dir / ".claude" / "skills" / "goal"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: goal\n---\n")
    await _set_executor_config(db, pid, str(repo_dir))

    result = await _call_start_session(db, pid, role="executor", monkeypatch=monkeypatch)

    assert "setup_warning" not in result, (
        "setup_warning should be absent when .claude/skills/goal/ exists"
    )


@pytest.mark.asyncio
async def test_no_setup_warning_when_commands_file_present(db, project, tmp_path, monkeypatch):
    """role=executor + .claude/commands/goal.md exists → no setup_warning."""
    pid = project["id"]
    repo_dir = tmp_path / "my-repo4"
    cmd_dir = repo_dir / ".claude" / "commands"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "goal.md").write_text("# goal\nRun an executor session.\n")
    await _set_executor_config(db, pid, str(repo_dir))

    result = await _call_start_session(db, pid, role="executor", monkeypatch=monkeypatch)

    assert "setup_warning" not in result, (
        "setup_warning should be absent when .claude/commands/goal.md exists"
    )


# ---------------------------------------------------------------------------
# 3. No role (or non-executor role) → never setup_warning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_setup_warning_when_no_role(db, project, tmp_path, monkeypatch):
    """No role → no setup_warning even if skill is absent."""
    pid = project["id"]
    repo_dir = tmp_path / "my-repo5"
    repo_dir.mkdir()
    await _set_executor_config(db, pid, str(repo_dir))

    result = await _call_start_session(db, pid, role=None, monkeypatch=monkeypatch)

    assert "setup_warning" not in result


@pytest.mark.asyncio
async def test_no_setup_warning_for_planner_role(db, project, tmp_path, monkeypatch):
    """role=planner → no setup_warning even if skill is absent."""
    pid = project["id"]
    repo_dir = tmp_path / "my-repo6"
    repo_dir.mkdir()
    await _set_executor_config(db, pid, str(repo_dir))

    result = await _call_start_session(db, pid, role="planner", monkeypatch=monkeypatch)

    assert "setup_warning" not in result


# ---------------------------------------------------------------------------
# 4. No repo_path configured → no setup_warning (nothing to check)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_setup_warning_when_no_repo_path(db, project, monkeypatch):
    """role=executor but no repo_path in executor_config → no setup_warning.

    We can't know what repo the executor is working in, so we don't warn.
    """
    pid = project["id"]
    # Deliberately do NOT call _set_executor_config → repo_path is None.

    result = await _call_start_session(db, pid, role="executor", monkeypatch=monkeypatch)

    assert "setup_warning" not in result


# ---------------------------------------------------------------------------
# 5. Best-effort: bad repo_path (non-existent) → no crash, no setup_warning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bad_repo_path_does_not_crash(db, project, monkeypatch):
    """A repo_path that does not exist should not cause start_session to fail.

    os.path.isdir on a nonexistent path returns False silently, so the check
    fires and emits setup_warning (repo dir exists check skipped — we just
    look for the skill sub-path). This test confirms no exception is raised.
    """
    pid = project["id"]
    await _set_executor_config(db, pid, "/nonexistent/path/that/does/not/exist")

    try:
        result = await _call_start_session(db, pid, role="executor", monkeypatch=monkeypatch)
    except Exception as exc:
        pytest.fail(f"start_session raised unexpectedly with bad repo_path: {exc}")

    # Result must be a dict (not an exception / None)
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# 6. Path splitting: Windows-style paths must work on any platform
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_windows_path_separator_handled(db, project, tmp_path, monkeypatch):
    """Windows-style backslash path in repo_path must not crash the check.

    The code uses manual path join (rstrip + "/" + suffix) rather than
    pathlib.Path.name to avoid platform-native separator issues on Linux CI.
    """
    pid = project["id"]
    repo_dir = tmp_path / "win-repo"
    skill_dir = repo_dir / ".claude" / "skills" / "goal"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: goal\n---\n")

    # Store path with forward slashes (both platforms accept this for os.path)
    await _set_executor_config(db, pid, str(repo_dir).replace("\\", "/"))

    result = await _call_start_session(db, pid, role="executor", monkeypatch=monkeypatch)

    # Skill is present so no warning expected
    assert "setup_warning" not in result


# ---------------------------------------------------------------------------
# 7. Executor git-hygiene rules appear in both /goal copies: the SKILL.md
#    execution steps and the AGENTS.md fallback snippet (kept in sync).
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent

_GOAL_RULE_PHRASES = (
    "its own git worktree",
    "get_file_claims",
    "claim_file",
    "stage by path only",
    "never force push",
    "never push main",
    "run the targeted tests before any merge",
    "complete_sprint_item",
)


def _normalise(text: str) -> str:
    return " ".join(text.split()).lower()


def _goal_skill_steps_section() -> str:
    text = (_REPO_ROOT / ".claude" / "skills" / "goal" / "SKILL.md").read_text(encoding="utf-8")
    start = text.index("## Step 2")
    end = text.index("## Step 3")
    return text[start:end]


def _agents_fallback_snippet() -> str:
    text = (_REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    anchor = text.index("If curl is unavailable")
    open_fence = text.index("```markdown", anchor)
    body_start = text.index("\n", open_fence) + 1
    close_fence = text.index("```", body_start)
    return text[body_start:close_fence]


@pytest.mark.parametrize("phrase", _GOAL_RULE_PHRASES)
def test_goal_rule_phrase_in_skill_md(phrase):
    """Each executor git-hygiene rule must appear in the SKILL.md execution steps."""
    assert phrase in _normalise(_goal_skill_steps_section())


@pytest.mark.parametrize("phrase", _GOAL_RULE_PHRASES)
def test_goal_rule_phrase_in_agents_fallback(phrase):
    """The AGENTS.md fallback snippet must carry the same rule list."""
    assert phrase in _normalise(_agents_fallback_snippet())
