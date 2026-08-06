"""Tests for ee8a6af1 — generate_handoff fails closed on stale sprint/
dependency references instead of serializing them.

Root cause (2026-08-04 incident): a canonical goal handoff for the live
v0.2.6 board serialized ``depends_on`` references
(``addc47eb-0da9-407f-95cc-79fe8b0fda03``,
``d092d2a4-7d3e-40c4-a463-33ceed570b44``) that were absent from the live
board — never validated against a fresh project+version snapshot before
rendering/token-binding.

Covers:
  - meridian/db/board_snapshot.py: get_project_item_index (flat, ALL-statuses
    existence + dependency-edge index) and find_stale_reference_ids (pure
    classification: live vs. stale, and why).
  - meridian/handoff.py: generate_handoff raises HandoffStaleReferenceError
    and writes NOTHING to disk when a stale depends_on edge is present, for
    EVERY mode (goal/starter/full/delta) — the regression matrix explicitly
    required by the sprint-item notes: pending, in_progress, done, missing
    dependency, merged-away item, and two-project isolation.
  - The three transports that catch HandoffEvidenceRequired (mcp/handler.py,
    mcp/stdio_handler.py, routes/handoff.py) get the same structured-refusal
    treatment for HandoffStaleReferenceError (error code "STALE_REFERENCE").

ffd7269c adds a GENERATED-BOARD matrix (deterministically seeded, not
hand-picked single cases) proving the security-critical invariant named in
that sprint item's acceptance criteria: no mode (goal/starter/full/delta)
can ever emit an executable, token-bound body from a board containing a
stale (missing), foreign-project, or merged-away dependency reference —
regardless of how many OTHER items on the same board are perfectly valid
(same-version, cross-version, or dependency-free) — and a clean board
(including legitimate cross-version edges) always mints a genuine,
independently-verifiable provenance token for every mode. It also proves
'planner' mode structurally never emits a <goal_token> at all (it is not a
token-bound executor body to begin with), so the invariant holds for that
mode by construction rather than by an exception path.
"""
from __future__ import annotations

import random
import re

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian.db import board_snapshot as board_snapshot_module
import meridian.server  # noqa: F401 — load the server before handler to avoid its import cycle
from meridian.mcp import handler as mcp_handler


# ---------------------------------------------------------------------------
# Pure helper: find_stale_reference_ids
# ---------------------------------------------------------------------------


def test_pure_no_dependency_reports_nothing():
    index = {"a": {"status": "pending", "depends_on": None, "merged_into": None}}
    assert board_snapshot_module.find_stale_reference_ids(index) == []


def test_pure_dependency_on_pending_is_live():
    index = {
        "a": {"status": "pending", "depends_on": "b", "merged_into": None},
        "b": {"status": "pending", "depends_on": None, "merged_into": None},
    }
    assert board_snapshot_module.find_stale_reference_ids(index) == []


def test_pure_dependency_on_in_progress_is_live():
    index = {
        "a": {"status": "pending", "depends_on": "b", "merged_into": None},
        "b": {"status": "in_progress", "depends_on": None, "merged_into": None},
    }
    assert board_snapshot_module.find_stale_reference_ids(index) == []


def test_pure_dependency_on_done_is_live():
    # 'done' must NOT be flagged stale — a completed dependency is resolved,
    # not stale. This is the specific false-positive the design must avoid.
    index = {
        "a": {"status": "pending", "depends_on": "b", "merged_into": None},
        "b": {"status": "done", "depends_on": None, "merged_into": None},
    }
    assert board_snapshot_module.find_stale_reference_ids(index) == []


def test_pure_dependency_missing_is_stale():
    index = {
        "a": {"status": "pending", "depends_on": "nonexistent", "merged_into": None},
    }
    stale = board_snapshot_module.find_stale_reference_ids(index)
    assert stale == [{"item_id": "a", "depends_on": "nonexistent", "reason": "missing"}]


def test_pure_dependency_merged_away_is_stale():
    index = {
        "a": {"status": "pending", "depends_on": "b", "merged_into": None},
        "b": {"status": "skipped", "depends_on": None, "merged_into": "survivor-1"},
    }
    stale = board_snapshot_module.find_stale_reference_ids(index)
    assert stale == [{
        "item_id": "a", "depends_on": "b",
        "reason": "merged_away", "merged_into": "survivor-1",
    }]


def test_pure_results_sorted_deterministically():
    index = {
        "z": {"status": "pending", "depends_on": "missing-1", "merged_into": None},
        "a": {"status": "pending", "depends_on": "missing-2", "merged_into": None},
    }
    stale = board_snapshot_module.find_stale_reference_ids(index)
    assert [s["item_id"] for s in stale] == ["a", "z"]


# ---------------------------------------------------------------------------
# get_project_item_index — DB-backed existence + dependency-edge index
# ---------------------------------------------------------------------------


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


@pytest.mark.asyncio
async def test_index_includes_done_items_unlike_build_board_snapshot(db):
    pid = await _project(db, "stale-ref-index-done")
    done_item = await db_module.add_sprint_item(db, pid, "v1", "will complete")
    await db_module.complete_sprint_item(db, pid, done_item["id"])

    index = await db_module.get_project_item_index(db, pid)
    assert done_item["id"] in index
    assert index[done_item["id"]]["status"] == "done"

    # Contrast: build_board_snapshot deliberately EXCLUDES done items.
    snap = await db_module.build_board_snapshot(db, pid)
    assert done_item["id"] not in {it["id"] for it in snap["items"]}


@pytest.mark.asyncio
async def test_index_scoped_to_project_excludes_other_projects(db):
    pid_a = await _project(db, "stale-ref-index-proj-a")
    pid_b = await _project(db, "stale-ref-index-proj-b")
    item_b = await db_module.add_sprint_item(db, pid_b, "v1", "lives in project B")

    index_a = await db_module.get_project_item_index(db, pid_a)
    assert item_b["id"] not in index_a

    index_b = await db_module.get_project_item_index(db, pid_b)
    assert item_b["id"] in index_b


@pytest.mark.asyncio
async def test_index_scoped_to_version_excludes_other_versions(db):
    pid = await _project(db, "stale-ref-index-version")
    v1_item = await db_module.add_sprint_item(db, pid, "v1", "in v1")
    v2_item = await db_module.add_sprint_item(db, pid, "v2", "in v2")

    index_v1 = await db_module.get_project_item_index(db, pid, version="v1")
    assert v1_item["id"] in index_v1
    assert v2_item["id"] not in index_v1

    index_unscoped = await db_module.get_project_item_index(db, pid)
    assert v1_item["id"] in index_unscoped
    assert v2_item["id"] in index_unscoped


@pytest.mark.asyncio
async def test_index_reflects_merged_into(db):
    pid = await _project(db, "stale-ref-index-merged")
    source = await db_module.add_sprint_item(db, pid, "v1", "will be merged away")
    await db_module.merge_sprint_items(db, pid, [source["id"]], "merge survivor")

    index = await db_module.get_project_item_index(db, pid)
    assert index[source["id"]]["status"] == "skipped"
    assert index[source["id"]]["merged_into"]  # survivor id, non-empty


# ---------------------------------------------------------------------------
# generate_handoff — regression matrix (fail closed on stale depends_on)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_handoff_succeeds_dependency_pending(db, tmp_path):
    pid = await _project(db, "stale-ref-goal-dep-pending")
    parent = await db_module.add_sprint_item(db, pid, "v1", "parent still pending")
    await db_module.add_sprint_item(db, pid, "v1", "child", depends_on=parent["id"])

    _, content, _ = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
    )
    assert content  # rendered normally, no exception


@pytest.mark.asyncio
async def test_generate_handoff_succeeds_dependency_in_progress(db, tmp_path):
    pid = await _project(db, "stale-ref-goal-dep-in-progress")
    parent = await db_module.add_sprint_item(db, pid, "v1", "parent claimed")
    await db_module.claim_sprint_item(db, pid, parent["id"])
    await db_module.add_sprint_item(db, pid, "v1", "child", depends_on=parent["id"])

    _, content, _ = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
    )
    assert content


@pytest.mark.asyncio
async def test_generate_handoff_succeeds_dependency_done(db, tmp_path):
    """The specific false-positive to guard against: a completed dependency
    must NOT be treated as stale just because it's excluded from the
    non-done board_snapshot."""
    pid = await _project(db, "stale-ref-goal-dep-done")
    parent = await db_module.add_sprint_item(db, pid, "v1", "parent already done")
    await db_module.complete_sprint_item(db, pid, parent["id"])
    await db_module.add_sprint_item(db, pid, "v1", "child", depends_on=parent["id"])

    _, content, _ = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
    )
    assert content


@pytest.mark.asyncio
async def test_generate_handoff_raises_on_missing_dependency(db, tmp_path):
    pid = await _project(db, "stale-ref-goal-dep-missing")
    child = await db_module.add_sprint_item(
        db, pid, "v1", "child of a ghost", depends_on="ghost-item-id-does-not-exist",
    )

    with pytest.raises(handoff_module.HandoffStaleReferenceError) as excinfo:
        await handoff_module.generate_handoff(
            db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
        )
    exc = excinfo.value
    assert exc.project_id == pid
    assert exc.version is None
    assert exc.stale_references == [{
        "item_id": child["id"],
        "depends_on": "ghost-item-id-does-not-exist",
        "reason": "missing",
    }]
    # Fail closed: nothing rendered/written for this call.
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_generate_handoff_raises_on_merged_away_dependency(db, tmp_path):
    pid = await _project(db, "stale-ref-goal-dep-merged")
    source = await db_module.add_sprint_item(db, pid, "v1", "will be merged away")
    survivor = await db_module.merge_sprint_items(
        db, pid, [source["id"]], "merge survivor title",
    )
    # A stray depends_on still points at the PRE-merge id.
    await db_module.add_sprint_item(
        db, pid, "v1", "child of a merged-away item", depends_on=source["id"],
    )

    with pytest.raises(handoff_module.HandoffStaleReferenceError) as excinfo:
        await handoff_module.generate_handoff(
            db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
        )
    exc = excinfo.value
    assert len(exc.stale_references) == 1
    ref = exc.stale_references[0]
    assert ref["depends_on"] == source["id"]
    assert ref["reason"] == "merged_away"
    assert ref["merged_into"] == survivor["id"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_generate_handoff_raises_on_two_project_isolation(db, tmp_path):
    pid_a = await _project(db, "stale-ref-goal-isolation-a")
    pid_b = await _project(db, "stale-ref-goal-isolation-b")
    foreign = await db_module.add_sprint_item(db, pid_b, "v1", "real item, wrong project")
    await db_module.add_sprint_item(
        db, pid_a, "v1", "child referencing a foreign-project id",
        depends_on=foreign["id"],
    )

    with pytest.raises(handoff_module.HandoffStaleReferenceError) as excinfo:
        await handoff_module.generate_handoff(
            db, pid_a, str(tmp_path), skip_ai_summary=True, mode="goal",
        )
    exc = excinfo.value
    assert exc.project_id == pid_a
    assert exc.stale_references[0]["depends_on"] == foreign["id"]
    assert exc.stale_references[0]["reason"] == "missing"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["goal", "starter", "full", "delta"])
async def test_generate_handoff_stale_reference_blocks_every_mode(db, tmp_path, mode):
    """ee8a6af1 explicitly requires the check to run 'before rendering any
    mode' — prove it for all four executable modes (planner is out of scope:
    a separate rendering pipeline with no sprint-item id serialization)."""
    pid = await _project(db, f"stale-ref-every-mode-{mode}")
    await db_module.add_sprint_item(
        db, pid, "v1", "child of a ghost", depends_on="ghost-item-id-does-not-exist",
    )

    out_dir = tmp_path / mode
    out_dir.mkdir()
    with pytest.raises(handoff_module.HandoffStaleReferenceError):
        await handoff_module.generate_handoff(
            db, pid, str(out_dir), skip_ai_summary=True, mode=mode,
        )
    assert list(out_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_generate_handoff_version_scoping_accepts_cross_version_dependency(
    db, tmp_path,
):
    """Cross-version dependencies are valid project-local external edges.

    The executable item list remains version-scoped, while dependency
    identity is checked against the whole project so a valid prerequisite in
    another sprint bucket cannot suppress every handoff mode.
    """
    pid = await _project(db, "stale-ref-cross-version")
    parent_v2 = await db_module.add_sprint_item(db, pid, "v2", "parent lives in v2")
    child_v1 = await db_module.add_sprint_item(
        db, pid, "v1", "child in v1", depends_on=parent_v2["id"],
    )

    _, scoped_content, _ = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="goal", version="v1",
    )
    assert scoped_content
    assert child_v1["id"] in scoped_content

    # Unscoped (version=None) sees the whole project — not stale.
    _, content, _ = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
    )
    assert content


# ---------------------------------------------------------------------------
# Transport-layer structured refusals (mirrors HandoffEvidenceRequired
# coverage in test_cov_handoff.py / test_stdio_handoff_arg_parity.py)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_dispatch_stale_reference_returns_structured_error(db, tmp_path):
    pid = await _project(db, "stale-ref-mcp-dispatch")
    child = await db_module.add_sprint_item(
        db, pid, "v1", "child of a ghost", depends_on="ghost-item-id-does-not-exist",
    )

    result = await mcp_handler._handle_task_tools(
        "generate_handoff", {"project_id": pid, "mode": "goal"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    assert result["error"] == "STALE_REFERENCE"
    assert result["project_id"] == pid
    assert result["stale_references"][0]["item_id"] == child["id"]
    assert "content" not in result
    assert "path" not in result


def test_routes_handoff_endpoint_returns_structured_422(client):
    project = client.post("/projects", json={"name": "stale-ref-http"}).json()
    pid = project["id"]
    client.post(
        f"/projects/{pid}/sprint-items",
        json={
            "version": "v1", "title": "child of a ghost",
            "depends_on": "ghost-item-id-does-not-exist",
        },
    )

    r = client.post(f"/projects/{pid}/handoff", json={"mode": "goal"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "STALE_REFERENCE"
    assert detail["project_id"] == pid
    assert detail["stale_references"][0]["reason"] == "missing"


# ---------------------------------------------------------------------------
# ffd7269c — generated-board matrix. Deterministically seeded (not hand-
# picked) board topologies, exercising every dependency-role category named
# in the sprint item's acceptance criteria (stale/missing, foreign-project,
# merged-away, valid same-version, valid cross-version, none) in
# combination on a SINGLE board, across every executable mode.
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"<goal_token>([^<]+)</goal_token>")


def _extract_token(text: str) -> str | None:
    m = _TOKEN_RE.search(text or "")
    return m.group(1).strip() if m else None


async def _count_handoff_tokens(db, project_id: str) -> int:
    async with db.execute(
        "SELECT COUNT(*) AS c FROM handoff_tokens WHERE project_id = ?",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return 0
    return row["c"] if isinstance(row, dict) else row[0]


# Every dependency role a generated item can be assigned. "stale" roles
# (ghost/foreign/merged_away) must ALWAYS block every mode; the rest must
# never block on their own.
_DEP_ROLES = (
    "none",
    "valid_same_version",
    "valid_cross_version",
    "ghost",
    "foreign",
    "merged_away",
)
_STALE_ROLES = {"ghost", "foreign", "merged_away"}


async def _build_generated_board(
    db, rng: random.Random, home_pid: str, foreign_pid: str, size: int,
) -> tuple[list[dict[str, str]], bool]:
    """Populate ``home_pid`` with ``size`` randomly-roled dependent items on
    top of four fixed anchors (v1-valid, v2-valid, a merged-away source, and
    a real item that lives entirely in a different project). Returns
    ``(items, expected_stale)``.
    """
    anchor_v1 = await db_module.add_sprint_item(db, home_pid, "v1", "anchor v1")
    anchor_v2 = await db_module.add_sprint_item(db, home_pid, "v2", "anchor v2")
    merged_source = await db_module.add_sprint_item(db, home_pid, "v1", "will be merged")
    await db_module.merge_sprint_items(db, home_pid, [merged_source["id"]], "merge survivor")
    foreign_item = await db_module.add_sprint_item(db, foreign_pid, "v1", "lives in another project")

    items: list[dict[str, str]] = []
    expected_stale = False
    for i in range(size):
        role = rng.choice(_DEP_ROLES)
        version = rng.choice(["v1", "v2"])
        depends_on: str | None
        if role == "none":
            depends_on = None
        elif role == "valid_same_version":
            depends_on = anchor_v1["id"] if version == "v1" else anchor_v2["id"]
        elif role == "valid_cross_version":
            # Deliberately mismatched bucket: a live, real, project-local
            # dependency in a DIFFERENT version than the item itself — a
            # legitimate external edge, not staleness (ee8a6af1's own
            # distinction between project-wide identity and version-scoped
            # executable view).
            depends_on = anchor_v2["id"] if version == "v1" else anchor_v1["id"]
        elif role == "ghost":
            depends_on = f"ghost-{rng.randrange(10**9)}"
            expected_stale = True
        elif role == "foreign":
            depends_on = foreign_item["id"]
            expected_stale = True
        else:  # merged_away
            depends_on = merged_source["id"]
            expected_stale = True
        # force=True: the generated titles ("generated item N role=...")
        # deliberately share most of their words across the board (only the
        # index/role token differs), which trips add_sprint_item's near-
        # duplicate-title guard (b0d42ef6, >=60% word-set overlap) — a real
        # guard worth having for human-authored titles, but not the thing
        # this generator is exercising, so bypass it explicitly rather than
        # contort the titles into artificial uniqueness.
        item = await db_module.add_sprint_item(
            db, home_pid, version, f"generated item {i} role={role}",
            depends_on=depends_on, force=True,
        )
        items.append({"id": item["id"], "role": role, "version": version})
    return items, expected_stale


@pytest.mark.asyncio
@pytest.mark.parametrize("board_seed", range(8))
async def test_generated_board_matrix_no_mode_leaks_token_bound_body_from_bad_state(
    db, tmp_path, board_seed,
):
    """The core security invariant, proved over generated (not hand-picked)
    board topologies: whatever mix of none/valid-same-version/valid-cross-
    version/ghost/foreign/merged-away dependency roles a board happens to
    have, EVERY executable mode (goal/starter/full/delta) must:

    * refuse outright (HandoffStaleReferenceError), write NOTHING to disk,
      and mint NO new handoff_tokens row, whenever ANY item on the board has
      a stale/foreign/merged-away dependency — even when most other items
      on the same board are perfectly valid (mixed state, not all-bad); and
    * otherwise succeed with non-empty content and a genuine, independently
      verifiable provenance token, including when the only "unusual" edges
      are legitimate cross-version dependencies within the same project.
    """
    rng = random.Random(20260805_000 + board_seed)  # fixed seed -> deterministic
    home_pid = await _project(db, f"generated-board-home-{board_seed}")
    foreign_pid = await _project(db, f"generated-board-foreign-{board_seed}")
    size = rng.randint(2, 6)
    _items, expected_stale = await _build_generated_board(
        db, rng, home_pid, foreign_pid, size,
    )

    for mode in ("goal", "starter", "full", "delta"):
        out_dir = tmp_path / f"seed{board_seed}-{mode}"
        out_dir.mkdir()
        tokens_before = await _count_handoff_tokens(db, home_pid)

        if expected_stale:
            with pytest.raises(handoff_module.HandoffStaleReferenceError) as excinfo:
                await handoff_module.generate_handoff(
                    db, home_pid, str(out_dir), skip_ai_summary=True, mode=mode,
                )
            assert excinfo.value.project_id == home_pid
            assert excinfo.value.stale_references, "must report WHICH edges are stale"
            assert list(out_dir.iterdir()) == [], (
                f"mode={mode} must write nothing to disk on refusal"
            )
            tokens_after = await _count_handoff_tokens(db, home_pid)
            assert tokens_after == tokens_before, (
                f"mode={mode} must mint NO provenance token for a refused handoff "
                f"(board_seed={board_seed}, size={size})"
            )
        else:
            _path, content, _amended = await handoff_module.generate_handoff(
                db, home_pid, str(out_dir), skip_ai_summary=True, mode=mode,
            )
            assert content, f"mode={mode} must render on a clean board"
            token = _extract_token(content)
            assert token, (
                f"mode={mode} must embed a genuine provenance token on a clean "
                f"board (board_seed={board_seed}, size={size})"
            )
            verify = await handoff_module.verify_handoff_token(db, token, home_pid)
            assert verify == {"valid": True, "reason": "ok"}, (
                f"mode={mode}'s minted token must be genuine and scoped to "
                f"this exact project: {verify}"
            )


@pytest.mark.asyncio
async def test_planner_mode_never_emits_token_bound_body_even_with_stale_references(
    db, tmp_path,
):
    """Planner mode is deliberately EXCLUDED from the stale-reference check
    (it is a directive planning-session prompt, not an executor /goal — see
    HandoffStaleReferenceError's own docstring and
    test_generate_handoff_stale_reference_blocks_every_mode above). That
    exclusion is only safe because planner mode structurally never produces
    a token-bound executable body in the first place: prove it directly,
    on a board that WOULD block every other mode, so the invariant "no mode
    can emit an executable token-bound body from stale state" is verified
    for planner by construction (no token ever minted) rather than merely
    assumed from the fact that it is untested here.
    """
    pid = await _project(db, "stale-ref-planner-no-token")
    await db_module.add_sprint_item(
        db, pid, "v1", "child of a ghost", depends_on="ghost-item-id-does-not-exist",
    )

    tokens_before = await _count_handoff_tokens(db, pid)
    _path, content, _amended = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="planner",
    )
    assert content
    assert _extract_token(content) is None, (
        "planner mode must never embed a <goal_token> — it is not an "
        "executable, token-bound body"
    )
    tokens_after = await _count_handoff_tokens(db, pid)
    assert tokens_after == tokens_before, (
        "planner mode must never mint a provenance token, stale board or not"
    )

    # And every OTHER mode on this exact same (still-stale) board still
    # refuses outright, confirming planner's silence is a structural
    # property of that mode, not evidence the stale-reference check itself
    # was accidentally weakened.
    for mode in ("goal", "starter", "full", "delta"):
        out_dir = tmp_path / f"planner-contrast-{mode}"
        out_dir.mkdir()
        with pytest.raises(handoff_module.HandoffStaleReferenceError):
            await handoff_module.generate_handoff(
                db, pid, str(out_dir), skip_ai_summary=True, mode=mode,
            )
