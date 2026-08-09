"""Tests for sprint item 49e06bcb — lightweight worker execution classes +
deterministic routing.

Covers the two locked symbols for this item:
  * meridian/dispatcher.py::_worker_prompt (+ its new helper
    _classify_worker_execution) — the routing DECISION.
  * meridian/enqueue.py::_run_worker (+ its new helper
    _route_worker_execution and the WorkerExecutionClass descriptors) —
    consumes the decision and executes accordingly.

Design under test, in one sentence: an item's title opts it into the
lightweight DETERMINISTIC_WORKER class (targeted verification / evidence /
bookkeeping) via a small, explicit prefix allowlist; everything else stays
on the full-Claude-session SESSION_WORKER class, which is byte-identical
to this feature's pre-existing behavior. Both classes share IDENTICAL
subprocess/PID-lease/timeout/status-transition machinery in _run_worker —
only their task-log prefixes differ.
"""

from __future__ import annotations

import asyncio
import sys

import pytest
import pytest_asyncio

from meridian import db as db_module
from meridian import dispatcher as dispatcher_module
from meridian import enqueue as enqueue_module
from meridian.enqueue import _run_worker


@pytest_asyncio.fixture
async def project(db):
    proj = await db_module.create_project(db, "lightweight-worker-proj")
    return proj["id"]


@pytest_asyncio.fixture
async def session(db, project):
    sess = await db_module.register_session(db, project, "s")
    return sess["id"]


# Stub worker commands mirroring tests/test_core.py's _OK_WORKER /
# _FAIL_WORKER conventions, so behavior stays comparable without depending
# on the real `claude` CLI being installed.
_OK_WORKER = [sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1])"]
_FAIL_WORKER = [
    sys.executable,
    "-c",
    "import sys; sys.stderr.write('boom'); sys.exit(2)",
]


# ---------------------------------------------------------------------------
# _classify_worker_execution (meridian/dispatcher.py)
# ---------------------------------------------------------------------------


def test_classify_defaults_to_session_for_ordinary_titles():
    for title in ["Do thing", "FEAT: add widget", "fix bug", "", "   "]:
        item = {"id": "x", "title": title}
        assert (
            dispatcher_module._classify_worker_execution(item)
            == dispatcher_module.SESSION_WORKER_CLASS
        )


def test_classify_defaults_to_session_when_title_missing():
    assert (
        dispatcher_module._classify_worker_execution({"id": "x"})
        == dispatcher_module.SESSION_WORKER_CLASS
    )


@pytest.mark.parametrize(
    "title",
    [
        "VERIFY: rerun the test suite",
        "EVIDENCE: collect coverage numbers",
        "BOOKKEEPING: close out stale leases",
        "AUDIT: confirm receipts exist",
        "CHECK: confirm migration applied",
        # case-insensitive
        "verify: lowercase should still route",
    ],
)
def test_classify_routes_tagged_titles_to_deterministic(title):
    item = {"id": "x", "title": title}
    assert (
        dispatcher_module._classify_worker_execution(item)
        == dispatcher_module.DETERMINISTIC_WORKER_CLASS
    )


def test_classify_prefix_must_be_at_start_of_title():
    # The tag appearing mid-title must NOT trigger deterministic routing —
    # only a genuine leading tag counts, so an implementation item that
    # merely *mentions* VERIFY in its title stays SESSION.
    item = {"id": "x", "title": "FEAT: add a VERIFY: step to onboarding"}
    assert (
        dispatcher_module._classify_worker_execution(item)
        == dispatcher_module.SESSION_WORKER_CLASS
    )


# ---------------------------------------------------------------------------
# _worker_prompt (meridian/dispatcher.py) — routing marker + backward compat
# ---------------------------------------------------------------------------


def test_worker_prompt_session_item_has_no_marker_and_is_unchanged():
    """49e06bcb must not alter the prompt for any item that predates it."""
    item = {"id": "abc123", "title": "Do thing", "resources": ["fileA", "fileB"]}
    prompt = dispatcher_module._worker_prompt(item, "proj-1")
    assert not prompt.startswith("[worker-class:")
    assert "abc123" in prompt
    assert "Do thing" in prompt
    assert "fileA" in prompt and "fileB" in prompt
    assert "proj-1" in prompt


def test_worker_prompt_session_item_no_resources_omits_resources_word():
    item = {"id": "abc123", "title": "Do thing"}
    prompt = dispatcher_module._worker_prompt(item, "proj-1")
    assert not prompt.startswith("[worker-class:")
    assert "abc123" in prompt
    assert "resources" not in prompt.lower()


def test_worker_prompt_deterministic_item_carries_marker_line():
    item = {"id": "vrf-1", "title": "VERIFY: rerun test suite"}
    prompt = dispatcher_module._worker_prompt(item, "proj-2")
    lines = prompt.split("\n")
    assert lines[0] == "[worker-class: deterministic]"
    assert "vrf-1" in prompt
    assert "VERIFY: rerun test suite" in prompt
    assert "proj-2" in prompt
    # deterministic prompt asks for scoped verification, not open-ended work
    assert "complete_sprint_item" in prompt
    assert "claim_sprint_item" in prompt


def test_worker_prompt_deterministic_item_includes_resources():
    item = {
        "id": "vrf-2",
        "title": "AUDIT: confirm receipts",
        "resources": ["symbol:meridian/x.py::f"],
    }
    prompt = dispatcher_module._worker_prompt(item, "proj-3")
    assert prompt.startswith("[worker-class: deterministic]\n")
    assert "symbol:meridian/x.py::f" in prompt


# ---------------------------------------------------------------------------
# _route_worker_execution (meridian/enqueue.py) — marker parsing
# ---------------------------------------------------------------------------


def test_route_worker_execution_no_marker_is_session_and_unchanged():
    for prompt in ["hello world", "trigger failure", "", "multi\nline\nprompt"]:
        cls, effective = enqueue_module._route_worker_execution(prompt)
        assert cls is enqueue_module.SESSION_WORKER
        assert effective == prompt  # byte-identical — no marker to strip


def test_route_worker_execution_strips_recognized_marker():
    prompt = dispatcher_module._worker_prompt(
        {"id": "vrf-3", "title": "VERIFY: x"}, "proj-4"
    )
    cls, effective = enqueue_module._route_worker_execution(prompt)
    assert cls is enqueue_module.DETERMINISTIC_WORKER
    assert not effective.startswith("[worker-class:")
    assert "vrf-3" in effective


def test_route_worker_execution_requires_exact_marker_match():
    # Similar-but-not-exact marker text must NOT be treated as a match.
    prompt = "[worker-class: deterministic] extra text\nrest of prompt"
    cls, effective = enqueue_module._route_worker_execution(prompt)
    assert cls is enqueue_module.SESSION_WORKER
    assert effective == prompt


# ---------------------------------------------------------------------------
# _run_worker (meridian/enqueue.py) — end-to-end execution per class
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_worker_plain_prompt_is_session_class_unchanged(db, session, project):
    """Regression guard: a marker-free prompt behaves exactly as before 49e06bcb."""
    task = await db_module.log_task(db, session, project, "t", "pending")
    await _run_worker(db, task["id"], "hello world", _OK_WORKER, timeout=10)

    updated = await db_module.get_task(db, task["id"])
    assert updated["status"] == "done"
    assert updated["description"].startswith(enqueue_module.RESULT_PREFIX)
    assert "hello world" in updated["description"]
    # Lease preserved: PID was recorded during the run.
    assert updated["worker_pid"] is not None


@pytest.mark.asyncio
async def test_run_worker_deterministic_prompt_uses_deterministic_prefixes(
    db, session, project
):
    item = {"id": "vrf-4", "title": "BOOKKEEPING: record decision"}
    prompt = dispatcher_module._worker_prompt(item, project)
    task = await db_module.log_task(db, session, project, "t", "pending")

    await _run_worker(db, task["id"], prompt, _OK_WORKER, timeout=10)

    updated = await db_module.get_task(db, task["id"])
    assert updated["status"] == "done"
    assert updated["description"].startswith(enqueue_module.DETERMINISTIC_RESULT_PREFIX)
    # The internal routing marker must never leak into the persisted
    # description or (transitively, since _OK_WORKER echoes argv[1] back)
    # the subprocess's own stdout.
    assert "[worker-class:" not in updated["description"]
    assert "vrf-4" in updated["description"]
    # Lease preserved for the deterministic class too.
    assert updated["worker_pid"] is not None


@pytest.mark.asyncio
async def test_run_worker_deterministic_failure_uses_deterministic_error_prefix(
    db, session, project
):
    item = {"id": "vrf-5", "title": "CHECK: confirm state"}
    prompt = dispatcher_module._worker_prompt(item, project)
    task = await db_module.log_task(db, session, project, "t", "pending")

    await _run_worker(db, task["id"], prompt, _FAIL_WORKER, timeout=10)

    updated = await db_module.get_task(db, task["id"])
    assert updated["status"] == "failed"
    assert updated["description"].startswith(enqueue_module.DETERMINISTIC_ERROR_PREFIX)
    assert "exit code 2" in updated["description"]
    assert "boom" in updated["description"]


@pytest.mark.asyncio
async def test_run_worker_deterministic_missing_binary_fails_same_as_session(
    db, session, project
):
    """Failure policy (fail closed on spawn error) is identical across classes."""
    item = {"id": "vrf-6", "title": "VERIFY: nothing"}
    prompt = dispatcher_module._worker_prompt(item, project)
    task = await db_module.log_task(db, session, project, "t", "pending")

    await _run_worker(
        db, task["id"], prompt, ["definitely-not-a-real-binary-49e06bcb"], timeout=10
    )

    updated = await db_module.get_task(db, task["id"])
    assert updated["status"] == "failed"
    assert updated["description"].startswith(enqueue_module.DETERMINISTIC_ERROR_PREFIX)
    assert "not found" in updated["description"]


@pytest.mark.asyncio
async def test_run_worker_deterministic_timeout_fails_same_as_session(
    db, session, project
):
    item = {"id": "vrf-7", "title": "AUDIT: hang forever"}
    prompt = dispatcher_module._worker_prompt(item, project)
    task = await db_module.log_task(db, session, project, "t", "pending")
    slow = [sys.executable, "-c", "import time; time.sleep(5)"]

    await _run_worker(db, task["id"], prompt, slow, timeout=0.5)

    updated = await db_module.get_task(db, task["id"])
    assert updated["status"] == "failed"
    assert updated["description"].startswith(enqueue_module.DETERMINISTIC_ERROR_PREFIX)
    assert "timed out" in updated["description"]


@pytest.mark.asyncio
async def test_run_worker_in_progress_uses_class_prompt_prefix(db, session, project):
    """The transient in_progress row is labeled with the resolved class too."""
    item = {"id": "vrf-8", "title": "EVIDENCE: gather logs"}
    prompt = dispatcher_module._worker_prompt(item, project)
    task = await db_module.log_task(db, session, project, "t", "pending")
    brief_sleep = [sys.executable, "-c", "import time; time.sleep(1.5)"]

    run_task = asyncio.create_task(
        _run_worker(db, task["id"], prompt, brief_sleep, timeout=10)
    )
    try:
        in_progress_desc = None
        for _ in range(60):
            await asyncio.sleep(0.05)
            row = await db_module.get_task(db, task["id"])
            if row and row["status"] == "in_progress":
                in_progress_desc = row["description"]
                break
        assert in_progress_desc is not None, "never observed in_progress status"
        assert in_progress_desc.startswith(enqueue_module.DETERMINISTIC_PROMPT_PREFIX)
        assert "[worker-class:" not in in_progress_desc
        assert "vrf-8" in in_progress_desc
    finally:
        await run_task

    updated = await db_module.get_task(db, task["id"])
    assert updated["status"] == "done"


# ---------------------------------------------------------------------------
# WorkerExecutionClass descriptors — shape sanity
# ---------------------------------------------------------------------------


def test_session_worker_reuses_original_module_prefixes():
    """SESSION_WORKER must be the pre-49e06bcb constants, unchanged."""
    assert enqueue_module.SESSION_WORKER.prompt_prefix == enqueue_module.PROMPT_PREFIX
    assert enqueue_module.SESSION_WORKER.result_prefix == enqueue_module.RESULT_PREFIX
    assert enqueue_module.SESSION_WORKER.error_prefix == enqueue_module.ERROR_PREFIX


def test_deterministic_worker_has_distinct_prefixes_from_session():
    det = enqueue_module.DETERMINISTIC_WORKER
    sess = enqueue_module.SESSION_WORKER
    assert det.name == "deterministic"
    assert sess.name == "session"
    assert det.prompt_prefix != sess.prompt_prefix
    assert det.result_prefix != sess.result_prefix
    assert det.error_prefix != sess.error_prefix
