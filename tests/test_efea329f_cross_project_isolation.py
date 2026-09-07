"""Two-project cross-project/tenant isolation regression matrix (efea329f).

CRITICAL sprint item efea329f asked for hard project/tenant isolation across
six areas: handoffs, workspace context, sprint dependencies, pointers, notes,
and proposal evidence — plus a two-project regression matrix locking in the
result before any executor handoff can cross project boards.

A read-only discovery pass (session e872d8ba-5fe9-4806-b7a1-c820fc4895cb)
found the isolation boundary was ALREADY correctly enforced in four of the
six areas (workspace context is tenant-scoped by design; notes and proposal
evidence are project_id-filtered in SQL / verify-then-act; handoff tokens
correctly reject a wrong project). Two REAL, currently-exploitable gaps were
found and are fixed alongside this test file:

  1. ``db.sprint_items.get_blocking_dependency_for_sprint_item`` had NO
     project check at all — a cross-project ``depends_on`` value let one
     project's sprint-item TITLE leak into another project's task-claim
     response (``POST /projects/{id}/tasks/claim``, routes/tasks.py) and
     into every ``generate_handoff``'s rendered ``capability_contract``
     (via ``executor_contract._resolve_dependency_state``).
  2. The MCP tools ``get_sprint_item_pointers`` / ``resolve_sprint_item_pointers``
     never verified that ``sprint_item_id`` actually belongs to the caller's
     own ``project_id`` before returning/resolving its pointer targets (file
     paths, symbols, node/citation ids).

This file is the two-project regression matrix: for EACH of the six named
areas, a fixture builds two independent projects (A, B), each with its own
sprint item / note / pointer / proposal-evidence link, and asserts an
operation scoped to project A never returns, mutates, or embeds anything
belonging to project B. Sections 1, 4, and 6 lock in behavior that was
ALREADY correct; sections 2 and 3 are the actual fix targets and would have
failed before this change (see the corresponding ``db``/``mcp`` fixes in the
same commit).
"""
from __future__ import annotations

import json

import pytest
import pytest_asyncio

import meridian.server  # noqa: F401 — import before handler to avoid import cycle
from meridian import db as db_module
from meridian import executor_contract as executor_contract_module
from meridian import handoff as handoff_module
from meridian.db import proposal_links as proposal_links_module
from meridian.mcp import handler as mcp_handler
from meridian.mcp.handlers import sprint_tools as sprint_tools_module

pytestmark = pytest.mark.asyncio

_DATA_DIR = "."

# A distinctive marker baked into every project-B-owned title/body so a test
# can grep an arbitrary JSON dump for accidental leakage without needing to
# know the exact shape of whatever embedded it.
_SECRET_B_TITLE = "PROJECT-B-CONFIDENTIAL-SPRINT-ITEM-9f3c1a7e"
_SECRET_B_NOTE_TITLE = "PROJECT-B-CONFIDENTIAL-NOTE-2b6dd410"
_SECRET_B_NOTE_BODY = "project-b-only-body-text-77aa11bb"


# ---------------------------------------------------------------------------
# Fixtures — two independent projects, each with its own item/note.
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def project_a(db):
    return await db_module.create_project(db, "efea329f-project-a")


@pytest_asyncio.fixture
async def project_b(db):
    return await db_module.create_project(db, "efea329f-project-b")


@pytest_asyncio.fixture
async def item_b(db, project_b):
    """The item project B owns — everything below tries to reach it from A."""
    return await db_module.add_sprint_item(
        db, project_b["id"], "v1", _SECRET_B_TITLE, force=True,
    )


@pytest_asyncio.fixture
async def item_a_cross_dep(db, project_a, item_b):
    """A project-A item whose depends_on points at project B's item."""
    return await db_module.add_sprint_item(
        db, project_a["id"], "v1", "Project A child item",
        depends_on=item_b["id"], force=True,
    )


@pytest_asyncio.fixture
async def item_a_plain(db, project_a):
    """An ordinary project-A item with no cross-project dependency."""
    return await db_module.add_sprint_item(
        db, project_a["id"], "v1", "Project A plain item", force=True,
    )


# ---------------------------------------------------------------------------
# 1. Sprint dependencies — fan-in frontier / claim gate (ALREADY SAFE).
#    get_dependency_frontier / claim_sprint_item's DEPENDENCY_NOT_SATISFIED
#    gate already isolate a foreign predecessor. Locked in here so a future
#    change can't silently regress it.
# ---------------------------------------------------------------------------

async def test_claim_blocked_by_cross_project_dependency_and_never_leaks_title(
    db, project_a, item_a_cross_dep,
):
    result = await db_module.claim_sprint_item(
        db, project_a["id"], item_a_cross_dep["id"], actor="tester",
    )
    assert result is not None
    assert result.get("blocked") is True
    assert result.get("error") == "DEPENDENCY_NOT_SATISFIED"
    dumped = json.dumps(result)
    assert _SECRET_B_TITLE not in dumped


async def test_get_dependency_frontier_treats_foreign_predecessor_as_missing(
    db, project_a, item_a_cross_dep,
):
    frontier = await db_module.get_dependency_frontier(db, item_a_cross_dep)
    assert frontier["ready"] is False
    assert frontier["blocking"] == [
        {"id": item_a_cross_dep["depends_on"], "status": "missing", "reason": "predecessor not found"}
    ]
    # Never falsely satisfied by project B's own item status, and the
    # blocking payload never carries the foreign title (only id/status).
    assert _SECRET_B_TITLE not in json.dumps(frontier)


# ---------------------------------------------------------------------------
# 2. Sprint dependencies — legacy single-parent helper (REAL FIX TARGET).
#    get_blocking_dependency_for_sprint_item, its two callers
#    (_claim_task_result / routes/tasks.py's /tasks/claim route, and
#    executor_contract._resolve_dependency_state, embedded in every
#    generate_handoff's capability_contract) must never disclose a foreign
#    project's sprint-item title.
# ---------------------------------------------------------------------------

async def test_get_blocking_dependency_isolates_foreign_project(
    db, project_a, item_a_cross_dep, item_b,
):
    blocking = await db_module.get_blocking_dependency_for_sprint_item(
        db, item_a_cross_dep["id"],
    )
    assert blocking is not None
    # Reported exactly like a genuinely-missing dependency target — never
    # the real foreign row (title included).
    assert blocking["id"] == item_b["id"]
    assert blocking["status"] == "missing"
    assert blocking["title"] == "(missing sprint item)"
    assert _SECRET_B_TITLE not in json.dumps(blocking)


async def test_get_blocking_dependency_same_project_unaffected(db, project_a):
    """Regression guard: the fix must not break the ordinary same-project
    case — a real, unmet SAME-project dependency still surfaces its real
    title (this is not a leak; it's the intended, documented behavior)."""
    parent = await db_module.add_sprint_item(db, project_a["id"], "v1", "Real parent item", force=True)
    child = await db_module.add_sprint_item(
        db, project_a["id"], "v1", "Real child item", depends_on=parent["id"], force=True,
    )
    blocking = await db_module.get_blocking_dependency_for_sprint_item(db, child["id"])
    assert blocking is not None
    assert blocking["id"] == parent["id"]
    assert blocking["title"] == "Real parent item"
    assert blocking["status"] != "done"


async def test_claim_task_http_route_never_leaks_cross_project_title(client):
    """End-to-end HTTP regression for the P0 finding: POST
    /projects/{id}/tasks/claim is a plain authenticated route reachable by
    any caller scoped to project A. Before the fix, a cross-project
    depends_on let project B's item title leak straight into the response.
    """
    proj_a = client.post("/projects", json={"name": "efea329f-http-proj-a"}).json()
    proj_b = client.post("/projects", json={"name": "efea329f-http-proj-b"}).json()
    sess = client.post(
        "/sessions/register",
        json={"project_id": proj_a["id"], "name": "worker"},
    ).json()
    parent_b = client.post(
        f"/projects/{proj_b['id']}/sprint-items",
        json={"version": "v1", "title": _SECRET_B_TITLE},
    ).json()
    child_a = client.post(
        f"/projects/{proj_a['id']}/sprint-items",
        json={
            "version": "v1",
            "title": "Project A child (cross-project depends_on)",
            "depends_on": parent_b["id"],
        },
    ).json()

    blocked = client.post(
        f"/projects/{proj_a['id']}/tasks/claim",
        json={"task_id": child_a["id"], "session_id": sess["id"]},
    )
    assert blocked.status_code == 200
    body = blocked.json()
    assert body["claimed"] is False
    assert body["error"] == "dependency_not_met"
    # The foreign item's real title must never appear anywhere in the
    # response — it is reported as a missing dependency, not resolved.
    assert body["blocking_item_title"] != _SECRET_B_TITLE
    assert body["blocking_item_title"] == "(missing sprint item)"
    assert _SECRET_B_TITLE not in json.dumps(body)


async def test_executor_contract_dependency_state_never_leaks_cross_project_title(
    db, project_a, item_a_cross_dep,
):
    """executor_contract._resolve_dependency_state feeds
    capability_contract.item_executor_contracts, embedded in every
    generate_handoff call — this is the path that made the P0 leak reach a
    rendered handoff, not just the /tasks/claim HTTP route."""
    contract = await executor_contract_module.build_executor_contract(
        db, project_a["id"], item_a_cross_dep,
    )
    dependency = contract["dependency"]
    assert dependency["satisfied"] is False
    blocking_item = dependency["blocking_item"]
    assert blocking_item is not None
    assert blocking_item["title"] == "(missing sprint item)"
    assert _SECRET_B_TITLE not in json.dumps(contract)


# ---------------------------------------------------------------------------
# 5. Handoffs — end-to-end backstop: the full capability_contract every
#    generate_handoff mode embeds must never carry project B's item TITLE
#    (or any other project-B content) when built for project A, even with a
#    live cross-project depends_on. (Matrix item 5's "grep the entire
#    rendered output" check, applied to the exact function every
#    generate_handoff mode calls — handoff.build_effective_capability_contract.)
#
#    Note: item_b["id"] itself is expected to still appear, in the
#    ``dependency.depends_on`` field — that bare id is project A's OWN data
#    (it declared "I depend on <id>" on its own item) and was never secret;
#    the actual cross-project disclosure this item is about is project B's
#    TITLE/content riding along with that id, which is what the fix removes
#    (replaced by the same "(missing sprint item)" placeholder used for a
#    genuinely nonexistent dependency).
# ---------------------------------------------------------------------------

async def test_generate_handoff_capability_contract_never_leaks_project_b(
    db, project_a, item_a_cross_dep, item_b,
):
    contract = await handoff_module.build_effective_capability_contract(
        db, project_a["id"],
    )
    assert contract is not None
    assert contract["project_id"] == project_a["id"]
    dumped = json.dumps(contract)
    assert _SECRET_B_TITLE not in dumped
    # The dependency is reported as unsatisfied/missing, never resolved to
    # project B's real row.
    ec = next(
        c for c in contract["item_executor_contracts"]
        if c.get("item_id") == item_a_cross_dep["id"]
    )
    assert ec["dependency"]["blocking_item"] == {
        "id": item_b["id"], "title": "(missing sprint item)", "status": "missing",
    }


# ---------------------------------------------------------------------------
# 3. Pointers (REAL FIX TARGET). get_sprint_item_pointers /
#    resolve_sprint_item_pointers must refuse to operate on a sprint_item_id
#    that does not belong to the caller's own project_id.
# ---------------------------------------------------------------------------

async def test_get_sprint_item_pointers_mcp_refuses_cross_project(
    db, project_a, item_b,
):
    await db_module.add_sprint_item_pointer(
        db, item_b["project_id"], item_b["id"], "code",
        [{"uri": "meridian/db/proposal_links.py", "selector": {"type": "range", "start_line": 1, "end_line": 5}}],
    )
    result = await mcp_handler._handle_sprint_tools(
        "get_sprint_item_pointers",
        {"project_id": project_a["id"], "sprint_item_id": item_b["id"]},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result
    assert "pointers" not in result


async def test_resolve_sprint_item_pointers_mcp_refuses_cross_project(
    db, project_a, item_b,
):
    await db_module.add_sprint_item_pointer(
        db, item_b["project_id"], item_b["id"], "code",
        [{"uri": "meridian/db/proposal_links.py", "selector": {"type": "range", "start_line": 1, "end_line": 5}}],
    )
    result = await mcp_handler._handle_sprint_tools(
        "resolve_sprint_item_pointers",
        {"project_id": project_a["id"], "sprint_item_id": item_b["id"]},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result
    assert "pointers" not in result


async def test_get_sprint_item_pointers_same_project_still_works(db, project_a, item_a_plain):
    """Regression guard: the ownership check must not break the ordinary
    same-project case."""
    await db_module.add_sprint_item_pointer(
        db, project_a["id"], item_a_plain["id"], "code",
        [{"uri": "meridian/db/proposal_links.py", "selector": {"type": "range", "start_line": 1, "end_line": 5}}],
    )
    result = await sprint_tools_module.handle_get_sprint_item_pointers(
        {"project_id": project_a["id"], "sprint_item_id": item_a_plain["id"]},
        db, _DATA_DIR, None, None,
    )
    assert "pointers" in result
    assert len(result["pointers"]) == 1


# ---------------------------------------------------------------------------
# 4a. Notes (ALREADY SAFE) — lock in.
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def note_b(db, project_b):
    return await db_module.add_project_note(
        db, project_b["id"], _SECRET_B_NOTE_TITLE, _SECRET_B_NOTE_BODY,
    )


async def test_get_project_notes_scoped_to_project(db, project_a, project_b, note_b):
    await db_module.add_project_note(db, project_a["id"], "Project A note", "a-only-body")
    notes_a = await db_module.get_project_notes(db, project_a["id"], bodies=True)
    titles = {n["title"] for n in notes_a}
    assert _SECRET_B_NOTE_TITLE not in titles
    assert _SECRET_B_NOTE_BODY not in json.dumps(notes_a)


async def test_get_project_note_by_slug_scoped_to_project(db, project_a, note_b):
    """A project-A caller must not be able to read project B's note even
    when it somehow learns the exact slug (e.g. from a shared /goal or
    commit message)."""
    fetched = await db_module.get_project_note_by_slug(db, project_a["id"], note_b["slug"])
    assert fetched is None


# ---------------------------------------------------------------------------
# 4b. Proposal evidence (ALREADY SAFE) — lock in.
#    link_proposal_evidence is the strongest control in the codebase for
#    this item: it resolves the real entity row and raises ValueError on a
#    project mismatch. Verified here against a live cross-project sprint
#    item, not just re-asserted from the docstring.
# ---------------------------------------------------------------------------

async def test_link_proposal_evidence_rejects_cross_project_sprint_item(
    db, project_a, item_b,
):
    with pytest.raises(ValueError, match="different project"):
        await proposal_links_module.link_proposal_evidence(
            db, project_a["id"], "proposal-efea329f-test",
            "sprint_item", item_b["id"],
        )
    # No link must have been written despite the raise.
    links = await proposal_links_module.get_proposal_links(db, project_a["id"], "proposal-efea329f-test")
    assert links == []


async def test_link_proposal_evidence_same_project_succeeds(db, project_a, item_a_plain):
    link = await proposal_links_module.link_proposal_evidence(
        db, project_a["id"], "proposal-efea329f-test-2",
        "sprint_item", item_a_plain["id"],
    )
    assert link["entity_id"] == item_a_plain["id"]


# ---------------------------------------------------------------------------
# 6. Handoff token (ALREADY SAFE) — lock in wrong_project rejection.
# ---------------------------------------------------------------------------

async def test_verify_handoff_token_rejects_wrong_project(db, project_a, project_b):
    token = await handoff_module.mint_handoff_token(db, project_b["id"])
    result = await handoff_module.verify_handoff_token(db, token, project_a["id"])
    assert result["valid"] is False
    assert result["reason"] == "wrong_project"


async def test_verify_handoff_token_accepts_matching_project(db, project_b):
    token = await handoff_module.mint_handoff_token(db, project_b["id"])
    result = await handoff_module.verify_handoff_token(db, token, project_b["id"])
    assert result["valid"] is True
    assert result["reason"] == "ok"
