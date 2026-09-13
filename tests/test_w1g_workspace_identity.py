"""W1-G: Workspace identity + handoff coherence.

Source spec: workspace proposal 4f42eb2d ("Meridian research-execution
trust: workspace identity, policy precedence, pointer provenance, and
handoff state") — the DNABERT-incident postmortem. Sprint item
cb35149f-eb38-43d4-b120-dd4d51a04d7d decomposes it into 7 sub-items; this
file covers the 5 implemented in this pass:

* G1 — bind project_id to a canonical repository identity (a derived
  fingerprint, never a raw machine-local absolute path).
* G2 — start_session warns when the calling session's cwd does not match
  the project's registered repo identity.
* G3 — a stale project-level execution_mode='autonomous' must never
  silently outrank an explicit planner role or a pinned SCOPE decision.
* G4 — generate_handoff's planner-mode pending/in_progress split must come
  from ONE consistent snapshot, never two separately-timed queries that can
  disagree.
* G6 — set_active_repo failures carry an actionable diagnostic (run_id /
  tenant_id / cross_instance_owner), not just a bare string.

G5 (hosted pointer validation) and G7 (artifact readiness host-visibility)
were investigated but deliberately NOT implemented in this pass — see the
landing report for why (pointers.py/artifact_declaration.py's existence
checks have zero hosted/tunnel awareness across 8+ consumer call sites; a
safe fix needs to thread a three-state result through all of them, out of
scope for a single focused change).
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import executor_config as ec
from meridian import repo_scope


# ---------------------------------------------------------------------------
# G1 — compute_repo_identity: pure, deterministic, never leaks the raw path.
# ---------------------------------------------------------------------------

class TestComputeRepoIdentity:
    def test_deterministic_for_the_same_path(self):
        a = repo_scope.compute_repo_identity(r"C:\Users\alice\project")
        b = repo_scope.compute_repo_identity(r"C:\Users\alice\project")
        assert a == b

    def test_case_and_separator_insensitive(self):
        a = repo_scope.compute_repo_identity(r"C:\Repo\Project")
        b = repo_scope.compute_repo_identity("c:/repo/project/")
        assert a == b

    def test_different_paths_produce_different_identities(self):
        a = repo_scope.compute_repo_identity(r"C:\Users\alice\project")
        b = repo_scope.compute_repo_identity(r"C:\Users\alice\other-project")
        assert a != b

    def test_never_embeds_the_raw_path(self):
        identity = repo_scope.compute_repo_identity(r"C:\Users\alice\secret-project")
        assert identity is not None
        assert "alice" not in identity
        assert "Users" not in identity
        assert "\\" not in identity

    def test_includes_a_readable_basename(self):
        identity = repo_scope.compute_repo_identity(r"C:\Users\alice\paper")
        assert identity is not None
        assert identity.startswith("paper-")

    @pytest.mark.parametrize("bad", [None, "", "   "])
    def test_empty_or_missing_is_none(self, bad):
        assert repo_scope.compute_repo_identity(bad) is None


# ---------------------------------------------------------------------------
# G1 — db.set_project_repo_identity / get_project_repo_identity round trip.
# ---------------------------------------------------------------------------

class TestProjectRepoIdentityDb:
    @pytest.mark.asyncio
    async def test_round_trip(self, db):
        project = await db_module.create_project(db, "repo-identity-roundtrip")
        assert await db_module.get_project_repo_identity(db, project["id"]) is None

        updated = await db_module.set_project_repo_identity(
            db, project["id"], r"C:\Users\alice\project"
        )
        expected = repo_scope.compute_repo_identity(r"C:\Users\alice\project")
        assert updated["repo_identity"] == expected
        assert await db_module.get_project_repo_identity(db, project["id"]) == expected

    @pytest.mark.asyncio
    async def test_empty_repo_path_is_a_no_op(self, db):
        project = await db_module.create_project(db, "repo-identity-noop")
        result = await db_module.set_project_repo_identity(db, project["id"], "")
        assert result["repo_identity"] is None

    @pytest.mark.asyncio
    async def test_unknown_project_returns_none(self, db):
        assert await db_module.get_project_repo_identity(db, "no-such-project") is None


# ---------------------------------------------------------------------------
# G1 — migration: idempotent, adds the column.
# ---------------------------------------------------------------------------

class TestRepoIdentityMigration:
    @pytest.mark.asyncio
    async def test_migration_is_idempotent_and_adds_column(self):
        import aiosqlite
        from meridian.db import migrations as _mig

        conn = await aiosqlite.connect(":memory:")
        try:
            conn.row_factory = aiosqlite.Row
            await conn.execute(
                "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL)"
            )
            await conn.commit()
            await _mig._migrate_repo_identity(conn)
            # Re-run must be a no-op (idempotent) and not raise.
            await _mig._migrate_repo_identity(conn)
            async with conn.execute("PRAGMA table_info(projects)") as cur:
                cols = {r["name"] for r in await cur.fetchall()}
            assert "repo_identity" in cols
        finally:
            await conn.close()


# ---------------------------------------------------------------------------
# G3 — build_execution_policy: role/pinned-decision downgrade precedence.
# ---------------------------------------------------------------------------

class TestBuildExecutionPolicyScopeOverride:
    def test_omitting_role_and_decisions_is_byte_for_byte_unchanged(self):
        """Backward compatibility: every pre-G3 call site omits both new
        params and must see the EXACT pre-G3 dict shape (no new keys)."""
        policy = ec.build_execution_policy(None, execution_mode="autonomous")
        assert policy == {
            "execution_mode": "immediate",
            "max_planning_turns": ec.DEFAULT_MAX_PLANNING_TURNS_IMMEDIATE,
            "required_first_action": ec.REQUIRED_FIRST_ACTION_IMMEDIATE,
            "no_confirmation": True,
            "permitted_parallel_wave": True,
            "claim_before_edit": True,
            "genuine_blocker_escalation": ec.GENUINE_BLOCKER_ESCALATION_RULE,
        }

    def test_planner_role_downgrades_autonomous_to_relaxed(self):
        policy = ec.build_execution_policy(
            None, execution_mode="autonomous", role="planner",
        )
        assert policy["execution_mode"] == "relaxed"
        assert policy["no_confirmation"] is False
        assert policy["permitted_parallel_wave"] is False
        assert policy["downgraded_from_autonomous"] is True
        assert policy["downgrade_reason"] == "planner_role"

    def test_executor_role_does_not_downgrade(self):
        policy = ec.build_execution_policy(
            None, execution_mode="autonomous", role="executor",
        )
        assert policy["execution_mode"] == "immediate"
        assert "downgraded_from_autonomous" not in policy

    def test_pinned_scope_decision_downgrades_autonomous_to_relaxed(self):
        pinned = [
            {"id": "d1", "category": "TECHNICAL", "title": "unrelated"},
            {"id": "d2", "category": "SCOPE", "title": "one item at a time"},
        ]
        policy = ec.build_execution_policy(
            None, execution_mode="autonomous", pinned_decisions=pinned,
        )
        assert policy["execution_mode"] == "relaxed"
        assert policy["downgraded_from_autonomous"] is True
        assert policy["downgrade_reason"] == "pinned_scope_decision"
        assert policy["scope_decision_ids"] == ["d2"]

    def test_scope_category_is_case_insensitive(self):
        pinned = [{"id": "d1", "category": "scope"}]
        policy = ec.build_execution_policy(
            None, execution_mode="autonomous", pinned_decisions=pinned,
        )
        assert policy["downgrade_reason"] == "pinned_scope_decision"

    def test_no_scope_decisions_does_not_downgrade(self):
        pinned = [{"id": "d1", "category": "TECHNICAL"}]
        policy = ec.build_execution_policy(
            None, execution_mode="autonomous", pinned_decisions=pinned,
        )
        assert policy["execution_mode"] == "immediate"
        assert "downgraded_from_autonomous" not in policy

    def test_already_relaxed_mode_is_unaffected_by_role_or_decisions(self):
        """Nothing to downgrade FROM when the project is already interactive
        — no spurious downgrade_reason on an already-relaxed policy."""
        policy = ec.build_execution_policy(
            None, execution_mode="interactive", role="planner",
            pinned_decisions=[{"id": "d1", "category": "SCOPE"}],
        )
        assert policy["execution_mode"] == "relaxed"
        assert "downgraded_from_autonomous" not in policy

    def test_malformed_pinned_decisions_entries_are_ignored_not_fatal(self):
        pinned = ["not-a-dict", {"category": "SCOPE"}, {"id": "d1"}]
        policy = ec.build_execution_policy(
            None, execution_mode="autonomous", pinned_decisions=pinned,
        )
        # Neither malformed entry (no id, or non-dict) contributes an id —
        # no crash, and no downgrade without a real id'd SCOPE decision.
        assert policy["execution_mode"] == "immediate"


# ---------------------------------------------------------------------------
# G1/G2/G3 — start_session wiring: cwd binding/mismatch + role-aware policy.
# ---------------------------------------------------------------------------

class TestStartSessionWorkspaceIdentity:
    @pytest.mark.asyncio
    async def test_first_cwd_binds_the_project_repo_identity(self, db):
        from meridian import server as srv

        project = await db_module.create_project(db, "ws-identity-bind")
        result = await srv._dispatch_mcp_tool(
            "start_session",
            {
                "project_id": project["id"], "session_name": "s1",
                "cwd": r"C:\Users\alice\project",
            },
            db, "/tmp",
        )
        assert "cwd_mismatch_warning" not in result
        stored = await db_module.get_project_repo_identity(db, project["id"])
        assert stored == repo_scope.compute_repo_identity(r"C:\Users\alice\project")

    @pytest.mark.asyncio
    async def test_matching_cwd_on_a_later_call_has_no_warning(self, db):
        from meridian import server as srv

        project = await db_module.create_project(db, "ws-identity-match")
        await db_module.set_project_repo_identity(db, project["id"], r"C:\Users\alice\project")
        result = await srv._dispatch_mcp_tool(
            "start_session",
            {
                "project_id": project["id"], "session_name": "s2",
                "cwd": r"C:\Users\alice\project",
            },
            db, "/tmp",
        )
        assert "cwd_mismatch_warning" not in result

    @pytest.mark.asyncio
    async def test_mismatched_cwd_surfaces_a_warning(self, db):
        """The exact DNABERT-incident class of bug: the same project_id used
        from two different local checkouts must be flagged, not silent."""
        from meridian import server as srv

        project = await db_module.create_project(db, "ws-identity-mismatch")
        await db_module.set_project_repo_identity(db, project["id"], r"C:\Users\alice\project")
        result = await srv._dispatch_mcp_tool(
            "start_session",
            {
                "project_id": project["id"], "session_name": "s3",
                "cwd": r"C:\Users\alice\ChatGPT\project",
            },
            db, "/tmp",
        )
        assert "cwd_mismatch_warning" in result
        warning = result["cwd_mismatch_warning"]
        assert warning["expected_repo_identity"] == repo_scope.compute_repo_identity(
            r"C:\Users\alice\project"
        )
        assert warning["observed_repo_identity"] == repo_scope.compute_repo_identity(
            r"C:\Users\alice\ChatGPT\project"
        )
        # The stored identity must NOT be silently overwritten by the mismatch.
        assert await db_module.get_project_repo_identity(db, project["id"]) == (
            repo_scope.compute_repo_identity(r"C:\Users\alice\project")
        )

    @pytest.mark.asyncio
    async def test_omitting_cwd_is_a_complete_no_op(self, db):
        from meridian import server as srv

        project = await db_module.create_project(db, "ws-identity-omit-cwd")
        result = await srv._dispatch_mcp_tool(
            "start_session",
            {"project_id": project["id"], "session_name": "s4"},
            db, "/tmp",
        )
        assert "cwd_mismatch_warning" not in result
        assert await db_module.get_project_repo_identity(db, project["id"]) is None

    @pytest.mark.asyncio
    async def test_planner_role_downgrades_execution_policy_in_start_session(self, db):
        from meridian import server as srv

        project = await db_module.create_project(db, "ws-identity-planner-role")
        result = await srv._dispatch_mcp_tool(
            "start_session",
            {"project_id": project["id"], "session_name": "s5", "role": "planner"},
            db, "/tmp",
        )
        policy = result.get("execution_policy")
        assert policy is not None
        assert policy["execution_mode"] == "relaxed"
        assert policy["downgrade_reason"] == "planner_role"

    @pytest.mark.asyncio
    async def test_pinned_scope_decision_downgrades_execution_policy_in_start_session(self, db):
        from meridian import server as srv

        project = await db_module.create_project(db, "ws-identity-scope-decision")
        await db_module.pin_decision(
            db, project["id"], "One item at a time", "no auto-submission",
            category="SCOPE",
        )
        result = await srv._dispatch_mcp_tool(
            "start_session",
            {"project_id": project["id"], "session_name": "s6"},
            db, "/tmp",
        )
        policy = result.get("execution_policy")
        assert policy is not None
        assert policy["execution_mode"] == "relaxed"
        assert policy["downgrade_reason"] == "pinned_scope_decision"


# ---------------------------------------------------------------------------
# G4 — _generate_planner_handoff: one consistent snapshot, never contradictory.
# ---------------------------------------------------------------------------

class TestPlannerHandoffConsistentSnapshot:
    @pytest.mark.asyncio
    async def test_pending_and_in_progress_come_from_disjoint_buckets(self, db):
        from meridian import handoff as _ho

        project = await db_module.create_project(db, "planner-handoff-consistency")
        pending_item = await db_module.add_sprint_item(
            db, project["id"], "v1", "Pending item"
        )
        progress_item = await db_module.add_sprint_item(
            db, project["id"], "v1", "In-progress item"
        )
        await db_module.claim_sprint_item(
            db, project["id"], progress_item["id"], actor="claimer"
        )

        all_items = await db_module.get_sprint_items(db, project["id"])
        by_id = {it["id"]: it["status"] for it in all_items}
        assert by_id[pending_item["id"]] == "pending"
        assert by_id[progress_item["id"]] == "in_progress"

        _path, content = await _ho._generate_planner_handoff(db, project["id"], "/tmp")

        # Both items are rendered, each under exactly one section header.
        assert f"[pending] {pending_item['title']}" in content
        assert f"[in_progress] {progress_item['title']}" in content
        # And never cross-labeled — the item that IS in_progress never also
        # renders with a [pending] tag, and vice versa.
        assert f"[pending] {progress_item['title']}" not in content
        assert f"[in_progress] {pending_item['title']}" not in content

    @pytest.mark.asyncio
    async def test_single_snapshot_query_not_two_separate_status_queries(self, db, monkeypatch):
        """The actual regression this closes: pending_items/in_progress_items
        used to come from TWO separate get_sprint_items(status=...) calls —
        a race window where an item could double-count or vanish between
        them. Assert the fetch now happens exactly once."""
        from meridian import handoff as _ho

        project = await db_module.create_project(db, "planner-handoff-single-fetch")
        await db_module.add_sprint_item(db, project["id"], "v1", "Some item")

        call_count = {"n": 0}
        real_get_sprint_items = db_module.get_sprint_items

        async def _counting_get_sprint_items(*args, **kwargs):
            call_count["n"] += 1
            return await real_get_sprint_items(*args, **kwargs)

        monkeypatch.setattr(db_module, "get_sprint_items", _counting_get_sprint_items)
        await _ho._generate_planner_handoff(db, project["id"], "/tmp")
        assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# G6 — set_active_repo failure carries an actionable diagnostic.
# ---------------------------------------------------------------------------

class TestSetActiveRepoDiagnostics:
    def test_diagnostic_suffix_includes_run_id_and_owner(self):
        from meridian.mcp.handler import _set_active_repo_diagnostic_suffix

        suffix = _set_active_repo_diagnostic_suffix({"id": "tenant-1"})
        assert "run_id=" in suffix
        assert "tenant_id=" in suffix
        assert "cross_instance_owner=" in suffix
        assert "get_tunnel_diagnostics" in suffix

    def test_diagnostic_suffix_never_raises_on_bad_input(self):
        """Diagnostics are best-effort — a failure here must never mask the
        real set_active_repo error."""
        from meridian.mcp.handler import _set_active_repo_diagnostic_suffix

        # A tenant shape build_tunnel_diagnostics cannot introspect must
        # degrade to an empty suffix, not raise.
        suffix = _set_active_repo_diagnostic_suffix("not-a-tenant-dict")
        assert isinstance(suffix, str)

    @pytest.mark.asyncio
    async def test_not_connected_error_message_still_matches_and_is_enriched(self, db, monkeypatch, tmp_path):
        """Existing callers match on the substring 'tunnel not connected' —
        the enrichment must be purely additive (appended), never replacing
        that substring."""
        from meridian.mcp.handler import _dispatch_mcp_tool
        from meridian.routes import tunnel as tunnel_mod

        async def _not_connected(tenant_id: str, repo_path: str):
            return {"status": "not_connected", "message": "no active extract tunnel"}

        monkeypatch.setattr(tunnel_mod, "send_active_repo_control", _not_connected)

        fake_tenant = {"id": "tenant-diag-01"}
        with pytest.raises(ValueError, match="tunnel not connected") as exc_info:
            await _dispatch_mcp_tool(
                "set_active_repo",
                {"repo_path": "/home/user/repo"},
                db, str(tmp_path), tenant=fake_tenant,
            )
        assert "run_id=" in str(exc_info.value)
        assert "tenant-diag-01" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_tunnel_error_status_message_is_also_enriched(self, db, monkeypatch, tmp_path):
        """The second failure branch (status == 'error') gets the same
        diagnostic enrichment as 'not_connected', not just the first one."""
        from meridian.mcp.handler import _dispatch_mcp_tool
        from meridian.routes import tunnel as tunnel_mod

        async def _errored(tenant_id: str, repo_path: str):
            return {"status": "error", "message": "extract slot crashed"}

        monkeypatch.setattr(tunnel_mod, "send_active_repo_control", _errored)

        fake_tenant = {"id": "tenant-diag-02"}
        with pytest.raises(ValueError, match="tunnel error while switching repo") as exc_info:
            await _dispatch_mcp_tool(
                "set_active_repo",
                {"repo_path": "/home/user/repo"},
                db, str(tmp_path), tenant=fake_tenant,
            )
        assert "extract slot crashed" in str(exc_info.value)
        assert "run_id=" in str(exc_info.value)
        assert "tenant-diag-02" in str(exc_info.value)
