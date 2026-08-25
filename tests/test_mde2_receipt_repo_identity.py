"""MDE-2 — code-intel prospecting receipts bind to repo identity.

Extends the existing a8c0f3b7 receipt gate (tests/test_code_intel_guard.py)
with the identity-binding half of MDE-2: a receipt now records the repo
root/revision it was resolved against (``resolve_receipt_repo_root``),
excludes a resolved identity that lands inside another agent tool's isolated
scratch checkout (``.codex/worktrees/...`` -- ``is_contaminated_repo_path``),
and ``verify_code_intel_prospecting`` explicitly rejects a receipt that
resolves to a DIFFERENT repo (or claims a resolved file that isn't really
there under its own declared root) instead of silently trusting it as
free-floating "prospecting happened somewhere" evidence.

Graph-unavailable / stale-graph / BM25-fallback / successful-resolution
coverage for the underlying three-rung chain already lives in
tests/test_prospect_symbol_and_graph_staleness.py (2ce5bc76/d5e60791) -- not
duplicated here. This file covers only the NEW identity-binding surface.
"""
from __future__ import annotations

import json

import pytest

from meridian import db as db_module
from meridian import code_intel_receipt as _cir


# ---------------------------------------------------------------------------
# is_contaminated_repo_path / normalize_repo_root -- pure, no DB needed.
# ---------------------------------------------------------------------------

class TestContaminationDetection:
    def test_forward_slash_codex_worktrees_is_contaminated(self):
        assert _cir.is_contaminated_repo_path("/home/user/repo/.codex/worktrees/abc123")

    def test_back_slash_codex_worktrees_is_contaminated(self):
        assert _cir.is_contaminated_repo_path(r"C:\Users\dev\repo\.codex\worktrees\abc123")

    def test_leading_relative_codex_worktrees_is_contaminated(self):
        assert _cir.is_contaminated_repo_path(".codex/worktrees/xyz")

    def test_ordinary_repo_path_is_not_contaminated(self):
        assert not _cir.is_contaminated_repo_path(r"C:\Users\dev\Meridian\repository")

    def test_meridian_own_claude_worktrees_is_not_contaminated(self):
        """Meridian's OWN worktree convention (.claude/worktrees/...,
        worktree_cleanup.looks_like_worktree_path) is a legitimate,
        registered checkout -- must never be treated as contamination."""
        assert not _cir.is_contaminated_repo_path(
            r"C:\Users\dev\Meridian\repository\.claude\worktrees\wf_abc123"
        )

    def test_empty_and_none_are_not_contaminated(self):
        assert not _cir.is_contaminated_repo_path("")
        assert not _cir.is_contaminated_repo_path(None)

    def test_normalize_repo_root_is_stable_across_calls(self, tmp_path):
        a = _cir.normalize_repo_root(str(tmp_path))
        b = _cir.normalize_repo_root(str(tmp_path) + "\\")
        assert a == b
        assert a is not None

    def test_normalize_repo_root_none_for_empty(self):
        assert _cir.normalize_repo_root("") is None
        assert _cir.normalize_repo_root(None) is None


# ---------------------------------------------------------------------------
# resolve_receipt_repo_root -- preference order: explicit root_dir > session's
# registered worktree > default_repo_root > unresolved.
# ---------------------------------------------------------------------------

class TestResolveReceiptRepoRoot:
    @pytest.mark.asyncio
    async def test_explicit_root_dir_wins(self, db, tmp_path):
        ctx = await _cir.resolve_receipt_repo_root(
            db, session_id=None, root_dir=str(tmp_path),
            default_repo_root=str(tmp_path / "other"),
        )
        assert ctx["repo_root"] == _cir.normalize_repo_root(str(tmp_path))
        assert ctx["source"] == "explicit_root_dir"
        assert ctx["contaminated"] is False

    @pytest.mark.asyncio
    async def test_falls_back_to_default_when_no_root_dir_or_worktree(self, db, tmp_path):
        ctx = await _cir.resolve_receipt_repo_root(
            db, session_id="no-such-session", root_dir=None,
            default_repo_root=str(tmp_path),
        )
        assert ctx["repo_root"] == _cir.normalize_repo_root(str(tmp_path))
        assert ctx["source"] == "default_repo_root"

    @pytest.mark.asyncio
    async def test_fully_unresolved_when_nothing_given(self, db):
        ctx = await _cir.resolve_receipt_repo_root(
            db, session_id=None, root_dir=None, default_repo_root=None,
        )
        assert ctx["repo_root"] is None
        assert ctx["source"] == "unresolved"
        assert ctx["contaminated"] is False

    @pytest.mark.asyncio
    async def test_explicit_root_dir_flags_contamination(self, db, tmp_path):
        contaminated = tmp_path / ".codex" / "worktrees" / "abc"
        ctx = await _cir.resolve_receipt_repo_root(
            db, session_id=None, root_dir=str(contaminated),
        )
        assert ctx["contaminated"] is True

    @pytest.mark.asyncio
    async def test_registered_session_worktree_wins_over_default(self, db, tmp_path):
        project = await db_module.create_project(db, "repo-identity-worktree-proj")
        sess = await db_module.register_session(db, project["id"], "identity-sess")
        real_wt = tmp_path / "session-worktree"
        real_wt.mkdir()
        await db_module.register_worktree(
            db, sess["id"], project["id"], "item/x", str(real_wt),
        )
        ctx = await _cir.resolve_receipt_repo_root(
            db, session_id=sess["id"], root_dir=None,
            default_repo_root=str(tmp_path / "server-main-checkout"),
        )
        assert ctx["repo_root"] == _cir.normalize_repo_root(str(real_wt))
        assert ctx["source"] == "session_worktree"


# ---------------------------------------------------------------------------
# record_prospect_receipt -- writes repo_root/revision/resolved_file/
# source_hash into detail; refuses to write a contaminated identity at all.
# ---------------------------------------------------------------------------

class TestRecordReceiptRepoBinding:
    @pytest.mark.asyncio
    async def test_records_repo_root_in_detail(self, db, tmp_path):
        project = await db_module.create_project(db, "receipt-repo-root-proj")
        row = await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="prospect_symbol", query="Thing", root_dir=str(tmp_path),
        )
        assert row is not None
        detail = json.loads(row["detail"])
        assert detail["repo_root"] == _cir.normalize_repo_root(str(tmp_path))
        assert detail["repo_source"] == "explicit_root_dir"

    @pytest.mark.asyncio
    async def test_records_resolved_file_and_source_hash(self, db, tmp_path):
        f = tmp_path / "meridian_thing.py"
        f.write_text("class Thing:\n    pass\n", encoding="utf-8")
        project = await db_module.create_project(db, "receipt-file-hash-proj")
        row = await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="find_symbol", query="Thing", root_dir=str(tmp_path),
            resolved_file="meridian_thing.py", rung="serena",
        )
        detail = json.loads(row["detail"])
        assert detail["resolved_file"] == "meridian_thing.py"
        assert detail["rung"] == "serena"
        assert detail["source_hash"] is not None
        assert len(detail["source_hash"]) == 64  # sha256 hex digest

    @pytest.mark.asyncio
    async def test_missing_resolved_file_has_no_source_hash(self, db, tmp_path):
        project = await db_module.create_project(db, "receipt-missing-file-proj")
        row = await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="search_graph", query="Thing", root_dir=str(tmp_path),
            resolved_file="does_not_exist.py",
        )
        detail = json.loads(row["detail"])
        assert detail["source_hash"] is None

    @pytest.mark.asyncio
    async def test_contaminated_root_dir_refuses_to_write_a_receipt(self, db, tmp_path):
        contaminated = tmp_path / ".codex" / "worktrees" / "sibling-agent-checkout"
        project = await db_module.create_project(db, "receipt-contaminated-proj")
        row = await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="prospect_symbol", query="Thing", root_dir=str(contaminated),
        )
        assert row is None
        log = await db_module.get_action_audit_log(
            db, project_id=project["id"], event_type=_cir.RECEIPT_EVENT_TYPE,
        )
        assert log == []

    @pytest.mark.asyncio
    async def test_no_root_dir_still_writes_with_unresolved_identity(self, db):
        """Back-compat: existing callers that never pass root_dir (e.g. the
        pre-MDE-2 test suite) still get a receipt written -- unresolved
        identity is not itself a rejection reason."""
        project = await db_module.create_project(db, "receipt-no-root-proj")
        row = await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="prospect_symbol", query="Thing",
        )
        assert row is not None
        detail = json.loads(row["detail"])
        assert detail["repo_root"] is None


# ---------------------------------------------------------------------------
# find_recent_prospect_receipt_with_context -- the identity-aware lookup.
# ---------------------------------------------------------------------------

class TestFindReceiptWithContext:
    @pytest.mark.asyncio
    async def test_matching_repo_root_is_returned(self, db, tmp_path):
        project = await db_module.create_project(db, "find-match-proj")
        await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="prospect_symbol", query="Thing", root_dir=str(tmp_path),
        )
        result = await _cir.find_recent_prospect_receipt_with_context(
            db, project_id=project["id"], expected_repo_root=str(tmp_path),
        )
        assert result["receipt"] is not None
        assert result["wrong_repo_only"] is False

    @pytest.mark.asyncio
    async def test_different_repo_root_is_rejected_as_wrong_repo(self, db, tmp_path):
        recorded_root = tmp_path / "repo-a"
        recorded_root.mkdir()
        expected_root = tmp_path / "repo-b"
        expected_root.mkdir()
        project = await db_module.create_project(db, "find-wrong-repo-proj")
        await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="prospect_symbol", query="Thing", root_dir=str(recorded_root),
        )
        result = await _cir.find_recent_prospect_receipt_with_context(
            db, project_id=project["id"], expected_repo_root=str(expected_root),
        )
        assert result["receipt"] is None
        assert result["wrong_repo_only"] is True

    @pytest.mark.asyncio
    async def test_unresolved_receipt_identity_is_accepted_not_rejected(self, db, tmp_path):
        """A receipt with no resolved identity of its own (legacy row, or a
        tunnel call with no active-repo cache) can't be PROVEN wrong -- it is
        accepted rather than penalized, matching the module's documented
        'harden, do not overclaim' posture."""
        project = await db_module.create_project(db, "find-unresolved-proj")
        await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="prospect_symbol", query="Thing",  # no root_dir
        )
        result = await _cir.find_recent_prospect_receipt_with_context(
            db, project_id=project["id"], expected_repo_root=str(tmp_path),
        )
        assert result["receipt"] is not None
        assert result["wrong_repo_only"] is False

    @pytest.mark.asyncio
    async def test_resolved_file_missing_from_its_own_root_is_rejected(self, db, tmp_path):
        """'Wrong-body' rejection: the receipt's own declared repo_root
        doesn't actually contain the file it claims to have resolved --
        never trusted as valid prospecting evidence even though the repo
        root itself matches."""
        project = await db_module.create_project(db, "find-wrong-body-proj")
        await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="find_symbol", query="Thing", root_dir=str(tmp_path),
            resolved_file="nonexistent_module.py",
        )
        result = await _cir.find_recent_prospect_receipt_with_context(
            db, project_id=project["id"], expected_repo_root=str(tmp_path),
        )
        assert result["receipt"] is None
        assert result["wrong_repo_only"] is True

    @pytest.mark.asyncio
    async def test_resolved_file_present_passes(self, db, tmp_path):
        f = tmp_path / "real_module.py"
        f.write_text("x = 1\n", encoding="utf-8")
        project = await db_module.create_project(db, "find-real-body-proj")
        await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="find_symbol", query="Thing", root_dir=str(tmp_path),
            resolved_file="real_module.py",
        )
        result = await _cir.find_recent_prospect_receipt_with_context(
            db, project_id=project["id"], expected_repo_root=str(tmp_path),
        )
        assert result["receipt"] is not None

    @pytest.mark.asyncio
    async def test_no_expected_repo_root_keeps_old_single_row_behavior(self, db, tmp_path):
        project = await db_module.create_project(db, "find-no-filter-proj")
        await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="prospect_symbol", query="Thing", root_dir=str(tmp_path),
        )
        result = await _cir.find_recent_prospect_receipt_with_context(
            db, project_id=project["id"], expected_repo_root=None,
        )
        assert result["receipt"] is not None
        assert result["wrong_repo_only"] is False

    @pytest.mark.asyncio
    async def test_backcompat_find_recent_prospect_receipt_returns_bare_row(self, db, tmp_path):
        """find_recent_prospect_receipt (the pre-MDE-2 entry point) still
        returns a bare row-or-None -- unchanged call shape for existing
        callers (tool_discovery.py et al)."""
        project = await db_module.create_project(db, "find-backcompat-proj")
        await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="prospect_symbol", query="Thing", root_dir=str(tmp_path),
        )
        found = await _cir.find_recent_prospect_receipt(db, project_id=project["id"])
        assert found is not None and found["project_id"] == project["id"]
        wrong = await _cir.find_recent_prospect_receipt(
            db, project_id=project["id"], expected_repo_root=str(tmp_path / "elsewhere"),
        )
        assert wrong is None


# ---------------------------------------------------------------------------
# verify_code_intel_prospecting end-to-end with repo identity.
# ---------------------------------------------------------------------------

def _cap_receipt(**overrides):
    base = {
        "id": _cir.CODE_INTEL_CAPABILITY_ID,
        "purpose": "verify semantic code-intel prospecting happened before code edits",
        "required_tools": ["prospect_symbol"],
        "fallback_chain": [],
        "availability_policy": "required",
    }
    base.update(overrides)
    return base


def _inv(**overrides):
    base = {
        "tunnel_reachable": True,
        "builtin_tools": {"prospect_symbol", "start_session"},
        "plugins": {},
        "stdio_registry": {},
    }
    base.update(overrides)
    return base


class TestVerifyCodeIntelProspectingRepoIdentity:
    @pytest.mark.asyncio
    async def test_required_policy_rejects_receipt_from_a_different_repo(self, db, tmp_path):
        recorded_root = tmp_path / "wrong-checkout"
        recorded_root.mkdir()
        current_root = tmp_path / "current-checkout"
        current_root.mkdir()
        project = await db_module.create_project(db, "verify-wrong-repo-proj")
        await db_module.set_project_capability_manifest(
            db, project["id"], [_cap_receipt(availability_policy="required")],
        )
        await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-x",
            tool_name="prospect_symbol", query="thing", root_dir=str(recorded_root),
        )
        item = {"touches_resources": '["file:meridian/db/sprint_items.py"]', "claimed_at": None}
        result = await _cir.verify_code_intel_prospecting(
            db, None, project["id"], item, live_inventory=_inv(),
            root_dir=str(current_root),
        )
        assert result["applicable"] is True
        assert result["ok"] is False
        assert result["code"] == "CODE_INTEL_RECEIPT_WRONG_REPO"

    @pytest.mark.asyncio
    async def test_degraded_ok_policy_warns_instead_of_blocking_on_wrong_repo(self, db, tmp_path):
        recorded_root = tmp_path / "wrong-checkout"
        recorded_root.mkdir()
        current_root = tmp_path / "current-checkout"
        current_root.mkdir()
        project = await db_module.create_project(db, "verify-wrong-repo-degraded-proj")
        await db_module.set_project_capability_manifest(
            db, project["id"], [_cap_receipt(availability_policy="degraded_ok")],
        )
        await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-x",
            tool_name="prospect_symbol", query="thing", root_dir=str(recorded_root),
        )
        item = {"touches_resources": '["file:meridian/db/sprint_items.py"]', "claimed_at": None}
        result = await _cir.verify_code_intel_prospecting(
            db, None, project["id"], item, live_inventory=_inv(),
            root_dir=str(current_root),
        )
        assert result["ok"] is True
        assert result["degraded"] is True
        assert result["warning"]

    @pytest.mark.asyncio
    async def test_matching_repo_identity_passes(self, db, tmp_path):
        shared_root = tmp_path / "shared-checkout"
        shared_root.mkdir()
        project = await db_module.create_project(db, "verify-matching-repo-proj")
        await db_module.set_project_capability_manifest(
            db, project["id"], [_cap_receipt(availability_policy="required")],
        )
        await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-x",
            tool_name="prospect_symbol", query="thing", root_dir=str(shared_root),
        )
        item = {"touches_resources": '["file:meridian/db/sprint_items.py"]', "claimed_at": None}
        result = await _cir.verify_code_intel_prospecting(
            db, None, project["id"], item, live_inventory=_inv(),
            root_dir=str(shared_root),
        )
        assert result["ok"] is True
        assert result["receipt"] is not None

    @pytest.mark.asyncio
    async def test_no_root_dir_context_behaves_exactly_as_pre_mde2(self, db):
        """Omitting root_dir/default_repo_root (as every pre-MDE-2 caller
        does) keeps the exact pre-MDE-2 behavior: no identity filtering at
        all, since expected_repo_root resolves to None."""
        project = await db_module.create_project(db, "verify-no-context-proj")
        await db_module.set_project_capability_manifest(
            db, project["id"], [_cap_receipt(availability_policy="required")],
        )
        await _cir.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-x",
            tool_name="prospect_symbol", query="thing",
        )
        item = {"touches_resources": '["file:meridian/db/sprint_items.py"]', "claimed_at": None}
        result = await _cir.verify_code_intel_prospecting(
            db, None, project["id"], item, live_inventory=_inv(),
        )
        assert result["ok"] is True
        assert result["receipt"] is not None
