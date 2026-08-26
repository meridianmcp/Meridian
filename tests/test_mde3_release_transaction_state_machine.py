"""MDE-3 -- canonical change-set/release manifest + crash recovery state
machine (meridian/db/docx_merge.py).

Covers:
  - The explicit PREPARED -> STAGED -> PROMOTED -> VERIFIED -> DB_COMMITTED
    -> RELEASED happy path, plus RECOVERY_REQUIRED reachability and the two
    terminal outcomes (RELEASED/ABORTED).
  - valid_release_transition's pure transition-graph logic (skip-ahead
    refused, idempotent re-assertion allowed, no escape from a terminal
    state).
  - advance_release_state fail-closed behavior: an invalid transition is
    REFUSED and never recorded -- "no partial release presented as
    complete."
  - Crash injection at every boundary: open_release_transaction / advance_
    release_state simulate "the process died right after this call
    returned" by simply not calling the next step, then a FRESH read
    (get_release_transaction / resolve_release_recovery) from a NEW
    "session" proves the durable journal alone is enough to recover
    correctly -- no in-memory state assumed.
  - resolve_release_recovery's three-way hash decision (abort / finish_db_
    commit / require_human) -- the core "never guess, never restore a
    stale backup" contract.
  - Idempotent resume: open_release_transaction returns the SAME
    transaction on retry; advance_release_state re-asserting the current
    state is a safe no-op.
  - summarize_release_transactions -- the bounded evidence projection for
    handoff.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian.db import docx_merge as DM


# ---------------------------------------------------------------------------
# valid_release_transition -- pure transition-graph logic.
# ---------------------------------------------------------------------------

class TestValidReleaseTransition:
    def test_none_to_prepared_is_the_only_legal_start(self):
        assert DM.valid_release_transition(None, DM.RELEASE_STATE_PREPARED) is True
        assert DM.valid_release_transition(None, DM.RELEASE_STATE_STAGED) is False

    def test_happy_path_single_step_forward_is_legal(self):
        order = DM._RELEASE_STATE_ORDER
        for a, b in zip(order, order[1:]):
            assert DM.valid_release_transition(a, b) is True

    def test_skipping_a_state_is_illegal(self):
        assert DM.valid_release_transition(
            DM.RELEASE_STATE_PREPARED, DM.RELEASE_STATE_PROMOTED,
        ) is False
        assert DM.valid_release_transition(
            DM.RELEASE_STATE_STAGED, DM.RELEASE_STATE_RELEASED,
        ) is False

    def test_backward_transition_is_illegal(self):
        assert DM.valid_release_transition(
            DM.RELEASE_STATE_VERIFIED, DM.RELEASE_STATE_STAGED,
        ) is False

    def test_reasserting_current_state_is_idempotent_and_legal(self):
        for state in DM._RELEASE_STATE_ORDER:
            assert DM.valid_release_transition(state, state) is True

    def test_recovery_required_reachable_from_any_non_terminal_state(self):
        for state in DM._RELEASE_STATE_ORDER:
            if state in DM._TERMINAL_RELEASE_STATES:
                continue
            assert DM.valid_release_transition(state, DM.RELEASE_STATE_RECOVERY_REQUIRED) is True

    def test_aborted_reachable_directly_from_any_non_terminal_state(self):
        """An in-flight transaction can be explicitly abandoned without
        first passing through RECOVERY_REQUIRED (e.g. an explicit
        cancellation of a PREPARED transaction that never touched
        anything)."""
        for state in DM._RELEASE_STATE_ORDER:
            if state in DM._TERMINAL_RELEASE_STATES:
                continue
            assert DM.valid_release_transition(state, DM.RELEASE_STATE_ABORTED) is True

    def test_recovery_required_resolves_to_aborted_or_forward_states_only(self):
        assert DM.valid_release_transition(
            DM.RELEASE_STATE_RECOVERY_REQUIRED, DM.RELEASE_STATE_ABORTED,
        ) is True
        assert DM.valid_release_transition(
            DM.RELEASE_STATE_RECOVERY_REQUIRED, DM.RELEASE_STATE_DB_COMMITTED,
        ) is True
        assert DM.valid_release_transition(
            DM.RELEASE_STATE_RECOVERY_REQUIRED, DM.RELEASE_STATE_RELEASED,
        ) is True
        assert DM.valid_release_transition(
            DM.RELEASE_STATE_RECOVERY_REQUIRED, DM.RELEASE_STATE_STAGED,
        ) is False

    def test_no_transition_escapes_a_terminal_state(self):
        for target in DM._RELEASE_STATE_ORDER + [DM.RELEASE_STATE_RECOVERY_REQUIRED]:
            if target == DM.RELEASE_STATE_RELEASED:
                continue
            assert DM.valid_release_transition(DM.RELEASE_STATE_RELEASED, target) is False
            assert DM.valid_release_transition(DM.RELEASE_STATE_ABORTED, target) is False
        # Re-asserting the SAME terminal state is still fine (idempotent).
        assert DM.valid_release_transition(DM.RELEASE_STATE_RELEASED, DM.RELEASE_STATE_RELEASED) is True
        assert DM.valid_release_transition(DM.RELEASE_STATE_ABORTED, DM.RELEASE_STATE_ABORTED) is True

    def test_unknown_state_is_illegal(self):
        assert DM.valid_release_transition(DM.RELEASE_STATE_PREPARED, "BOGUS") is False


# ---------------------------------------------------------------------------
# open_release_transaction / get_release_transaction / resumability.
# ---------------------------------------------------------------------------

class TestOpenAndGetReleaseTransaction:
    @pytest.mark.asyncio
    async def test_open_creates_a_prepared_transaction(self, db):
        project = await db_module.create_project(db, "release-open-proj")
        result = await DM.open_release_transaction(
            db, "cs-1", "output/report.docx", project_id=project["id"],
        )
        assert result["opened"] is True
        assert result["resumed"] is False
        assert result["state"] == DM.RELEASE_STATE_PREPARED
        assert result["transaction_id"]

    @pytest.mark.asyncio
    async def test_open_is_resumable_not_duplicated(self, db):
        project = await db_module.create_project(db, "release-resume-proj")
        first = await DM.open_release_transaction(
            db, "cs-1", "output/report.docx", project_id=project["id"],
        )
        second = await DM.open_release_transaction(
            db, "cs-1", "output/report.docx", project_id=project["id"],
        )
        assert second["resumed"] is True
        assert second["transaction_id"] == first["transaction_id"]

    @pytest.mark.asyncio
    async def test_open_after_release_starts_a_new_transaction(self, db):
        """A TERMINAL transaction must never be silently reused -- a fresh
        change-set targeting the same file starts a genuinely new one."""
        project = await db_module.create_project(db, "release-new-after-terminal-proj")
        first = await DM.open_release_transaction(
            db, "cs-1", "output/report.docx", project_id=project["id"],
        )
        await DM.advance_release_state(
            db, first["transaction_id"], DM.RELEASE_STATE_ABORTED, project_id=project["id"],
        )
        second = await DM.open_release_transaction(
            db, "cs-1", "output/report.docx", project_id=project["id"],
        )
        assert second["resumed"] is False
        assert second["transaction_id"] != first["transaction_id"]

    @pytest.mark.asyncio
    async def test_get_unknown_transaction_returns_none(self, db):
        project = await db_module.create_project(db, "release-unknown-proj")
        assert await DM.get_release_transaction(db, "nonexistent-id", project_id=project["id"]) is None

    @pytest.mark.asyncio
    async def test_missing_change_set_id_or_path_is_invalid(self, db):
        project = await db_module.create_project(db, "release-invalid-proj")
        r1 = await DM.open_release_transaction(db, "", "x.docx", project_id=project["id"])
        r2 = await DM.open_release_transaction(db, "cs-1", "", project_id=project["id"])
        assert r1["opened"] is False
        assert r2["opened"] is False


# ---------------------------------------------------------------------------
# advance_release_state -- fail-closed transition enforcement.
# ---------------------------------------------------------------------------

class TestAdvanceReleaseState:
    @pytest.mark.asyncio
    async def test_happy_path_full_lifecycle(self, db):
        project = await db_module.create_project(db, "release-happy-path-proj")
        opened = await DM.open_release_transaction(
            db, "cs-1", "output/report.docx", base_hash="base123", project_id=project["id"],
        )
        tid = opened["transaction_id"]

        r1 = await DM.advance_release_state(
            db, tid, DM.RELEASE_STATE_STAGED, staged_path="/tmp/stage.tmp",
            staged_hash="staged-hash", project_id=project["id"],
        )
        assert r1["advanced"] is True

        r2 = await DM.advance_release_state(
            db, tid, DM.RELEASE_STATE_PROMOTED, post_hash="staged-hash", project_id=project["id"],
        )
        assert r2["advanced"] is True

        r3 = await DM.advance_release_state(db, tid, DM.RELEASE_STATE_VERIFIED, project_id=project["id"])
        assert r3["advanced"] is True

        r4 = await DM.advance_release_state(
            db, tid, DM.RELEASE_STATE_DB_COMMITTED, db_commit_ref="item-42",
            provenance_registered=True, project_id=project["id"],
        )
        assert r4["advanced"] is True

        r5 = await DM.advance_release_state(db, tid, DM.RELEASE_STATE_RELEASED, project_id=project["id"])
        assert r5["advanced"] is True

        final = await DM.get_release_transaction(db, tid, project_id=project["id"])
        assert final["state"] == DM.RELEASE_STATE_RELEASED
        assert final["staged_hash"] == "staged-hash"
        assert final["post_hash"] == "staged-hash"
        assert final["db_commit_ref"] == "item-42"
        assert final["provenance_registered"] is True
        assert final["history"] == DM._RELEASE_STATE_ORDER

    @pytest.mark.asyncio
    async def test_skipping_a_state_is_refused_and_not_recorded(self, db):
        """The core 'no partial release presented as complete' guarantee."""
        project = await db_module.create_project(db, "release-skip-refused-proj")
        opened = await DM.open_release_transaction(db, "cs-1", "x.docx", project_id=project["id"])
        tid = opened["transaction_id"]

        result = await DM.advance_release_state(
            db, tid, DM.RELEASE_STATE_RELEASED, project_id=project["id"],
        )
        assert result["advanced"] is False
        assert result["reason"] == "invalid_transition"

        # The refused transition must not have been recorded -- state is
        # UNCHANGED, still PREPARED.
        current = await DM.get_release_transaction(db, tid, project_id=project["id"])
        assert current["state"] == DM.RELEASE_STATE_PREPARED

    @pytest.mark.asyncio
    async def test_unknown_target_state_is_refused(self, db):
        project = await db_module.create_project(db, "release-unknown-state-proj")
        opened = await DM.open_release_transaction(db, "cs-1", "x.docx", project_id=project["id"])
        result = await DM.advance_release_state(
            db, opened["transaction_id"], "NOT_A_REAL_STATE", project_id=project["id"],
        )
        assert result["advanced"] is False
        assert result["reason"] == "unknown_state"

    @pytest.mark.asyncio
    async def test_advance_on_nonexistent_transaction_fails_cleanly(self, db):
        project = await db_module.create_project(db, "release-noexist-proj")
        result = await DM.advance_release_state(
            db, "nonexistent", DM.RELEASE_STATE_STAGED, project_id=project["id"],
        )
        assert result["advanced"] is False
        assert result["reason"] == "no_such_transaction"

    @pytest.mark.asyncio
    async def test_reasserting_current_state_is_idempotent(self, db):
        project = await db_module.create_project(db, "release-idempotent-proj")
        opened = await DM.open_release_transaction(db, "cs-1", "x.docx", project_id=project["id"])
        tid = opened["transaction_id"]
        r1 = await DM.advance_release_state(db, tid, DM.RELEASE_STATE_PREPARED, project_id=project["id"])
        assert r1["advanced"] is True
        current = await DM.get_release_transaction(db, tid, project_id=project["id"])
        assert current["state"] == DM.RELEASE_STATE_PREPARED

    @pytest.mark.asyncio
    async def test_cannot_advance_out_of_released(self, db):
        project = await db_module.create_project(db, "release-out-of-terminal-proj")
        opened = await DM.open_release_transaction(db, "cs-1", "x.docx", project_id=project["id"])
        tid = opened["transaction_id"]
        for state in DM._RELEASE_STATE_ORDER[1:]:
            await DM.advance_release_state(db, tid, state, project_id=project["id"])
        result = await DM.advance_release_state(
            db, tid, DM.RELEASE_STATE_STAGED, project_id=project["id"],
        )
        assert result["advanced"] is False


# ---------------------------------------------------------------------------
# Crash injection at every boundary + recovery -- the core acceptance bar.
#
# Each test opens a transaction, advances it to a specific state (simulating
# "the process crashed immediately after this call returned" by simply never
# calling the next step), then reads it back via a COMPLETELY FRESH call
# sequence (no reused local variables from "before the crash") to prove the
# durable journal alone drives the correct recovery decision.
# ---------------------------------------------------------------------------

class TestCrashRecoveryAtEveryBoundary:
    @pytest.mark.asyncio
    async def test_crash_after_prepared_current_equals_base_aborts(self, db):
        project = await db_module.create_project(db, "crash-prepared-proj")
        opened = await DM.open_release_transaction(
            db, "cs-1", "x.docx", base_hash="BASE", project_id=project["id"],
        )
        tid = opened["transaction_id"]
        # -- crash here: nothing else was ever called --

        # Fresh recovery pass: current on-disk hash is still BASE (nothing
        # was ever staged/promoted).
        result = await DM.resolve_release_recovery(db, tid, "BASE", project_id=project["id"])
        assert result["action"] == "abort"
        final = await DM.get_release_transaction(db, tid, project_id=project["id"])
        assert final["state"] == DM.RELEASE_STATE_ABORTED

    @pytest.mark.asyncio
    async def test_crash_after_staged_before_promote_aborts_safely(self, db):
        project = await db_module.create_project(db, "crash-staged-proj")
        opened = await DM.open_release_transaction(
            db, "cs-1", "x.docx", base_hash="BASE", project_id=project["id"],
        )
        tid = opened["transaction_id"]
        await DM.advance_release_state(
            db, tid, DM.RELEASE_STATE_STAGED, staged_path="/tmp/s.tmp",
            staged_hash="POST", post_hash="POST", project_id=project["id"],
        )
        # -- crash here: staged file written, canonical file_path untouched --

        result = await DM.resolve_release_recovery(db, tid, "BASE", project_id=project["id"])
        assert result["action"] == "abort"

    @pytest.mark.asyncio
    async def test_crash_after_promoted_before_verified_finishes_db_commit(self, db):
        """The promotion itself (the risky filesystem swap) already
        succeeded -- current_hash now matches post_hash. Recovery must NOT
        try to abort/undo a successful promotion; it resumes forward."""
        project = await db_module.create_project(db, "crash-promoted-proj")
        opened = await DM.open_release_transaction(
            db, "cs-1", "x.docx", base_hash="BASE", project_id=project["id"],
        )
        tid = opened["transaction_id"]
        await DM.advance_release_state(
            db, tid, DM.RELEASE_STATE_STAGED, staged_hash="POST", post_hash="POST",
            project_id=project["id"],
        )
        await DM.advance_release_state(db, tid, DM.RELEASE_STATE_PROMOTED, project_id=project["id"])
        # -- crash here: os.replace succeeded, canonical file now == POST --

        result = await DM.resolve_release_recovery(db, tid, "POST", project_id=project["id"])
        assert result["action"] == "finish_db_commit"
        final = await DM.get_release_transaction(db, tid, project_id=project["id"])
        assert final["state"] == DM.RELEASE_STATE_DB_COMMITTED

    @pytest.mark.asyncio
    async def test_crash_after_db_committed_before_released_finishes(self, db):
        project = await db_module.create_project(db, "crash-db-committed-proj")
        opened = await DM.open_release_transaction(
            db, "cs-1", "x.docx", base_hash="BASE", project_id=project["id"],
        )
        tid = opened["transaction_id"]
        await DM.advance_release_state(
            db, tid, DM.RELEASE_STATE_STAGED, staged_hash="POST", post_hash="POST",
            project_id=project["id"],
        )
        await DM.advance_release_state(db, tid, DM.RELEASE_STATE_PROMOTED, project_id=project["id"])
        await DM.advance_release_state(db, tid, DM.RELEASE_STATE_VERIFIED, project_id=project["id"])
        await DM.advance_release_state(
            db, tid, DM.RELEASE_STATE_DB_COMMITTED, db_commit_ref="ref-1", project_id=project["id"],
        )
        # -- crash here: DB commit landed, RELEASED never recorded --

        result = await DM.resolve_release_recovery(db, tid, "POST", project_id=project["id"])
        assert result["action"] == "finish_db_commit"
        final = await DM.get_release_transaction(db, tid, project_id=project["id"])
        assert final["state"] == DM.RELEASE_STATE_RELEASED
        # db_commit_ref recorded before the crash must survive recovery.
        assert final["db_commit_ref"] == "ref-1"

    @pytest.mark.asyncio
    async def test_crash_with_unknown_hash_requires_human_never_guesses(self, db):
        """The file on disk matches NEITHER base nor post -- e.g. a
        concurrent, unrelated write raced the transaction, or corruption.
        Must never guess or auto-restore a stale backup."""
        project = await db_module.create_project(db, "crash-unknown-proj")
        opened = await DM.open_release_transaction(
            db, "cs-1", "x.docx", base_hash="BASE", project_id=project["id"],
        )
        tid = opened["transaction_id"]
        await DM.advance_release_state(
            db, tid, DM.RELEASE_STATE_STAGED, staged_hash="POST", post_hash="POST",
            project_id=project["id"],
        )
        await DM.advance_release_state(db, tid, DM.RELEASE_STATE_PROMOTED, project_id=project["id"])

        result = await DM.resolve_release_recovery(
            db, tid, "SOMETHING-ELSE-ENTIRELY", project_id=project["id"],
        )
        assert result["action"] == "require_human"
        final = await DM.get_release_transaction(db, tid, project_id=project["id"])
        assert final["state"] == DM.RELEASE_STATE_RECOVERY_REQUIRED
        assert "UNRESOLVED" in (final.get("error") or "")

    @pytest.mark.asyncio
    async def test_none_current_hash_requires_human(self, db):
        """Caller couldn't even compute a current hash (e.g. the file
        vanished) -- must not silently treat that as anything."""
        project = await db_module.create_project(db, "crash-none-hash-proj")
        opened = await DM.open_release_transaction(
            db, "cs-1", "x.docx", base_hash="BASE", project_id=project["id"],
        )
        tid = opened["transaction_id"]
        result = await DM.resolve_release_recovery(db, tid, None, project_id=project["id"])
        assert result["action"] == "require_human"

    @pytest.mark.asyncio
    async def test_recovery_on_already_released_transaction_is_a_noop(self, db):
        project = await db_module.create_project(db, "crash-already-released-proj")
        opened = await DM.open_release_transaction(db, "cs-1", "x.docx", project_id=project["id"])
        tid = opened["transaction_id"]
        for state in DM._RELEASE_STATE_ORDER[1:]:
            await DM.advance_release_state(db, tid, state, project_id=project["id"])
        result = await DM.resolve_release_recovery(db, tid, "anything", project_id=project["id"])
        assert result["action"] == "already_terminal"

    @pytest.mark.asyncio
    async def test_recovery_decision_is_idempotent_on_repeat_calls(self, db):
        """The FIRST resolution decides + records the outcome (here:
        'abort', landing on the terminal ABORTED state). A SECOND call
        against the now-terminal transaction is still safe -- it reports
        'already_terminal' rather than re-deciding or erroring, which is
        the correct idempotent behavior for a transaction that has already
        been resolved."""
        project = await db_module.create_project(db, "crash-idempotent-recovery-proj")
        opened = await DM.open_release_transaction(
            db, "cs-1", "x.docx", base_hash="BASE", project_id=project["id"],
        )
        tid = opened["transaction_id"]
        first = await DM.resolve_release_recovery(db, tid, "BASE", project_id=project["id"])
        assert first["action"] == "abort"
        second = await DM.resolve_release_recovery(db, tid, "BASE", project_id=project["id"])
        assert second["action"] == "already_terminal"
        final = await DM.get_release_transaction(db, tid, project_id=project["id"])
        assert final["state"] == DM.RELEASE_STATE_ABORTED


# ---------------------------------------------------------------------------
# list_release_transactions / summarize_release_transactions -- handoff
# evidence.
# ---------------------------------------------------------------------------

class TestListAndSummarize:
    @pytest.mark.asyncio
    async def test_list_filters_by_state(self, db):
        project = await db_module.create_project(db, "release-list-proj")
        a = await DM.open_release_transaction(db, "cs-a", "a.docx", project_id=project["id"])
        b = await DM.open_release_transaction(db, "cs-b", "b.docx", project_id=project["id"])
        await DM.advance_release_state(db, b["transaction_id"], DM.RELEASE_STATE_STAGED, project_id=project["id"])

        prepared = await DM.list_release_transactions(
            db, project_id=project["id"], state=DM.RELEASE_STATE_PREPARED,
        )
        staged = await DM.list_release_transactions(
            db, project_id=project["id"], state=DM.RELEASE_STATE_STAGED,
        )
        assert {t["transaction_id"] for t in prepared} == {a["transaction_id"]}
        assert {t["transaction_id"] for t in staged} == {b["transaction_id"]}

    @pytest.mark.asyncio
    async def test_summarize_counts_and_flags_recovery_required(self, db):
        project = await db_module.create_project(db, "release-summarize-proj")
        ok = await DM.open_release_transaction(db, "cs-ok", "ok.docx", project_id=project["id"])
        for state in DM._RELEASE_STATE_ORDER[1:]:
            await DM.advance_release_state(db, ok["transaction_id"], state, project_id=project["id"])

        stuck = await DM.open_release_transaction(
            db, "cs-stuck", "stuck.docx", base_hash="BASE", project_id=project["id"],
        )
        await DM.resolve_release_recovery(
            db, stuck["transaction_id"], "WEIRD", project_id=project["id"],
        )

        transactions = await DM.list_release_transactions(db, project_id=project["id"])
        summary = DM.summarize_release_transactions(transactions)
        assert summary["transaction_count"] == 2
        assert summary["state_counts"][DM.RELEASE_STATE_RELEASED] == 1
        assert summary["state_counts"][DM.RELEASE_STATE_RECOVERY_REQUIRED] == 1
        assert len(summary["recovery_required"]) == 1
        assert summary["recovery_required"][0]["change_set_id"] == "cs-stuck"
        assert summary["all_released"] is False

    def test_summarize_empty_list(self):
        summary = DM.summarize_release_transactions([])
        assert summary["transaction_count"] == 0
        assert summary["all_released"] is False

    def test_summarize_all_released_true_when_every_transaction_terminal(self):
        transactions = [
            {"transaction_id": "1", "state": DM.RELEASE_STATE_RELEASED},
            {"transaction_id": "2", "state": DM.RELEASE_STATE_ABORTED},
        ]
        summary = DM.summarize_release_transactions(transactions)
        assert summary["all_released"] is True
