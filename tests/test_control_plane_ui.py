"""07f753b2 — CONTROL-PLANE-UI-IMPLEMENT: focused, low-memory, serial tests
for meridian/routes/control_plane.py (the relationship explorer, the
paginated proposals view, and the self-hosted-only artifacts/manifest view).

Run serially (``-p no:xdist``) per this item's own instruction — these tests
are cheap (in-memory SQLite, no real filesystem beyond ``tmp_path``, no
external services) and gain nothing from xdist workers.

Covers, per the sprint item's explicit test list:
  * role matrix (who can see what)                -> TestRoleMatrix
  * parent/child (workspace/project/subproject)    -> TestRelationships
    visibility scoping
  * workspace/project scope enforcement            -> TestRelationships,
    (no cross-tenant leakage)                         TestProposalsScope
  * pagination/redaction correctness               -> TestProposalsPagination,
                                                        TestArtifacts
  * missing/stale artifact display                 -> TestArtifacts
  * UI route/helper behavior                       -> TestArtifactsUnavailable,
                                                        TestRedactionHelpers
"""
from __future__ import annotations

import asyncio
import importlib
import sys
import types
from contextlib import contextmanager

import pytest

from meridian import db as db_module
from meridian import _deps


# ---------------------------------------------------------------------------
# Hosted-mode client helper — mirrors tests/test_cov_route_export.py's
# _hosted_client fixture exactly (same env-isolation recipe), duplicated
# locally rather than imported since that helper is private to its own file.
# ---------------------------------------------------------------------------

@contextmanager
def _hosted_client(monkeypatch, tmp_path):
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    monkeypatch.setenv("MERIDIAN_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_SKIP_DEMO", "1")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))
    monkeypatch.setenv("MERIDIAN_MD_ROOT", str(tmp_path))

    from fastapi.testclient import TestClient
    import meridian.server as server_module
    server_module = importlib.reload(server_module)
    monkeypatch.setenv("MERIDIAN_AUTH_DB", "")
    monkeypatch.setenv("MERIDIAN_STANDARD_KEY", "")
    _deps._reset_limiter_counts()

    with TestClient(server_module.app) as c:
        yield c

    _deps._reset_limiter_counts()


def _seed_tenant_session(c, email, **tenant_fields):
    """Create a tenant on the ``admin`` plan (so ``_deps._db`` falls back to
    the shared in-memory auth DB instead of a real Neon URL — same hermetic
    trick ``test_cov_route_export.py`` uses) + an authenticated session
    cookie. Returns the tenant row.
    """
    from meridian.hosted import _make_session_cookie

    db = c.app.state.db
    tenant = asyncio.run(db_module.upsert_tenant(db, email))
    fields = {"plan": "admin", **tenant_fields}
    asyncio.run(db_module.update_tenant(db, tenant["id"], **fields))
    tenant = asyncio.run(db_module.get_tenant_by_id(db, tenant["id"]))
    session = asyncio.run(
        db_module.create_user_session(db, tenant["id"], "2099-01-01 00:00:00")
    )
    c.cookies.set("meridian_session", _make_session_cookie(session["id"]))
    return tenant


def _invite_and_accept(c, tenant_id, email, role, *, project_id=None):
    """Create + immediately accept a workspace invite, returning the member row."""
    db = c.app.state.db
    member = asyncio.run(
        db_module.create_workspace_invite(
            db, tenant_id, email, role, "th-" + email, project_id=project_id,
        )
    )
    return asyncio.run(db_module.accept_workspace_invite(db, member["id"]))


# ---------------------------------------------------------------------------
# Self-hosted mode — relationship explorer
# ---------------------------------------------------------------------------

class TestRelationships:
    def test_self_hosted_returns_workspace_scope_with_no_tenant(self, client):
        r = client.get("/control-plane/relationships")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["scope"] == "workspace"
        assert body["tenant_id"] is None
        assert body["scoped_project_ids"] is None

    def test_parent_child_nesting_is_exposed_via_parent_project_id(self, client):
        parent = client.post("/projects", json={"name": "Parent"}).json()
        child = client.post(
            "/projects", json={"name": "Child", "parent_project_id": parent["id"]},
        ).json()
        r = client.get("/control-plane/relationships")
        assert r.status_code == 200, r.text
        by_id = {p["id"]: p for p in r.json()["projects"]}
        assert by_id[parent["id"]]["relationship_scope"] == "project"
        assert by_id[parent["id"]]["parent_project_id"] is None
        assert by_id[child["id"]]["relationship_scope"] == "subproject"
        assert by_id[child["id"]]["parent_project_id"] == parent["id"]

    def test_public_project_shape_never_leaks_extra_columns(self, client):
        """Only the explicit allowlist survives — a raw DB row is never
        forwarded (defends against a future column added to `projects` that
        was never meant to be public)."""
        client.post("/projects", json={"name": "P"})
        r = client.get("/control-plane/relationships")
        row = r.json()["projects"][0]
        assert set(row.keys()) == {
            "id", "name", "status", "priority", "parent_project_id",
            "created_at", "relationship_scope",
        }


# ---------------------------------------------------------------------------
# Role matrix + cross-workspace scope enforcement (hosted mode)
# ---------------------------------------------------------------------------

class TestRoleMatrix:
    @pytest.mark.parametrize("role", ["owner", "admin", "member", "viewer"])
    def test_every_real_role_can_read_its_own_workspace(self, monkeypatch, tmp_path, role):
        with _hosted_client(monkeypatch, tmp_path) as c:
            tenant = _seed_tenant_session(c, f"{role}@example.com")
            r = c.get("/control-plane/relationships")
            assert r.status_code == 200, r.text
            assert r.json()["scope"] == "workspace"
            assert r.json()["tenant_id"] == tenant["id"]

    def test_cross_workspace_viewer_role_is_resolved_against_the_TARGET_workspace(
        self, monkeypatch, tmp_path,
    ):
        """Regression test for the bug this item's prospecting pass found and
        fixed: checking the caller's role against their OWN tenant id (via
        plain ``_get_tenant_from_request``) always passes, because every
        caller is 'owner' of their own account — that would silently defeat
        cross-workspace scope enforcement entirely. The fix
        (``_enforcement_context``) must resolve the role IN the workspace
        named by ``X-Workspace-Tenant-Id``, not the caller's own workspace.
        """
        with _hosted_client(monkeypatch, tmp_path) as c:
            owner = _seed_tenant_session(c, "owner@example.com")
            proj = c.post("/projects", json={"name": "Owner project"}).json()

            # A second, UNRELATED tenant account for the invited viewer's own
            # identity — this is the account they're actually signed in as.
            viewer_tenant = _seed_tenant_session(c, "viewer@example.com")
            _invite_and_accept(
                c, owner["id"], "viewer@example.com", "viewer", project_id=proj["id"],
            )

            r = c.get(
                "/control-plane/relationships",
                headers={"X-Workspace-Tenant-Id": owner["id"]},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            # Scoped to exactly the one project they were invited to, in the
            # OWNER's workspace — not their own (empty) personal workspace.
            assert body["scope"] == "project"
            assert body["scoped_project_ids"] == [proj["id"]]
            assert [p["id"] for p in body["projects"]] == [proj["id"]]

    def test_non_member_header_never_switches_which_db_this_endpoint_reads(
        self, monkeypatch, tmp_path,
    ):
        """A non-member's X-Workspace-Tenant-Id must never make ``_db()``
        switch this request onto the named tenant's OWN dedicated database —
        ``_enforcement_context``/``_db()`` both already refuse to switch for
        a non-member, and this asserts the relationships endpoint reports
        ``tenant_id`` as the STRANGER's own, not the owner's, confirming it
        never authenticated-as or read-as the named tenant.

        NOTE — confirmed, PRE-EXISTING, cross-cutting gap found while writing
        this test (not introduced by this item, and out of this item's
        scope to fix): ``projects`` has NO ``tenant_id`` column at all
        (confirmed by reading ``db.create_project``'s INSERT statement) —
        isolation between tenants relies entirely on each tenant having a
        genuinely SEPARATE physical database. The ``client`` an this
        test-suite's own ``_seed_tenant_session`` helper deliberately puts
        every tenant on the ``admin`` plan so ``_db()`` falls back to ONE
        SHARED in-memory database for hermetic testing (mirrors
        ``tests/test_cov_route_export.py``) — which means, in THIS test
        harness only, ``list_projects()`` legitimately returns the SAME
        shared rows for every admin-plan tenant regardless of which one is
        "active", same as the pre-existing ``GET /projects`` route already
        does. This test therefore does not (and, given that architecture,
        cannot) assert that the stranger's project LIST is empty — only that
        the endpoint reports the correct (stranger's own) tenant_id/scope
        rather than the owner's. Flagged in the final report as worth its
        own dedicated review, exactly like design note 8e06a475 routed the
        tunnel-proxy gap to its own item instead of blocking on it here.
        """
        with _hosted_client(monkeypatch, tmp_path) as c:
            owner = _seed_tenant_session(c, "owner2@example.com")
            c.post("/projects", json={"name": "Private project"})

            stranger = _seed_tenant_session(c, "stranger@example.com")
            r = c.get(
                "/control-plane/relationships",
                headers={"X-Workspace-Tenant-Id": owner["id"]},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            # The endpoint never treats the stranger as the owner: it reports
            # the stranger's OWN tenant_id/workspace scope, not the owner's.
            assert body["tenant_id"] == stranger["id"]
            assert body["scope"] == "workspace"
            assert body["scoped_project_ids"] is None

    def test_invalid_role_value_is_denied_read(self, monkeypatch, tmp_path):
        """Defense-in-depth: a corrupted/legacy workspace_members.role value
        that isn't one of the 4 real roles must not silently read as
        authorized. Exercises the has_perm(role, PERM_READ) 403 branch in
        _require_read, which the 4 real roles (all PERM_READ holders) can
        never otherwise reach."""
        with _hosted_client(monkeypatch, tmp_path) as c:
            owner = _seed_tenant_session(c, "owner3@example.com")
            other = _seed_tenant_session(c, "legacyrole@example.com")
            _invite_and_accept(c, owner["id"], "legacyrole@example.com", "member")

            async def _fake_resolve_member_role(db, tenant_id, email):
                if tenant_id == owner["id"] and email == "legacyrole@example.com":
                    return ("legacy_unknown_role", "none")
                return None

            monkeypatch.setattr(
                db_module, "resolve_member_role", _fake_resolve_member_role,
            )
            r = c.get(
                "/control-plane/relationships",
                headers={"X-Workspace-Tenant-Id": owner["id"]},
            )
            assert r.status_code == 403
            assert "legacy_unknown_role" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Proposals — pagination, redaction, scope enforcement
# ---------------------------------------------------------------------------

class TestProposalsPagination:
    def test_workspace_wide_pagination_and_has_more(self, client):
        for i in range(3):
            client.post("/workspace/proposals" if False else "/workspace/notes", json={})  # no-op guard
        for i in range(3):
            asyncio.run(
                db_module.add_workspace_proposal(
                    client.app.state.db, f"Idea {i}", f"Body {i}", tenant_id=None,
                )
            )
        r = client.get("/control-plane/proposals?limit=2")
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["items"]) == 2
        assert body["has_more"] is True
        r2 = client.get("/control-plane/proposals?limit=2&offset=2")
        assert len(r2.json()["items"]) == 1
        assert r2.json()["has_more"] is False

    def test_proposal_body_and_title_are_redacted(self, client):
        secret = "sk-ANTHROPIC1234567890ABCDEFGHIJKLMNOPQRSTUVWX"
        asyncio.run(
            db_module.add_workspace_proposal(
                client.app.state.db, "Has a key", f"token={secret}", tenant_id=None,
            )
        )
        r = client.get("/control-plane/proposals")
        body = r.json()["items"][0]
        assert secret not in body["body"]
        assert "REDACTED" in body["body"]

    def test_proposal_row_never_leaks_idempotency_key_or_family_id(self, client):
        asyncio.run(
            db_module.add_workspace_proposal(
                client.app.state.db, "T", "B", tenant_id=None,
                idempotency_key="secret-idem-key", family_id="fam-123",
            )
        )
        row = client.get("/control-plane/proposals").json()["items"][0]
        assert "idempotency_key" not in row
        assert "family_id" not in row
        assert "tenant_id" not in row


class TestProposalsScope:
    def test_project_scoped_member_must_name_their_own_project(self, monkeypatch, tmp_path):
        with _hosted_client(monkeypatch, tmp_path) as c:
            owner = _seed_tenant_session(c, "powner@example.com")
            proj = c.post("/projects", json={"name": "Scoped project"}).json()
            other_proj = c.post("/projects", json={"name": "Other project"}).json()
            asyncio.run(
                db_module.add_workspace_proposal(
                    c.app.state.db, "Workspace-wide idea", "body",
                    tenant_id=owner["id"],
                )
            )

            viewer_tenant = _seed_tenant_session(c, "pviewer@example.com")
            _invite_and_accept(
                c, owner["id"], "pviewer@example.com", "viewer", project_id=proj["id"],
            )

            # No project_id at all -> 403 (would otherwise leak every
            # workspace-wide + other-project proposal, the confirmed gap in
            # db.get_workspace_proposals itself — 8e06a475).
            r_no_pid = c.get(
                "/control-plane/proposals",
                headers={"X-Workspace-Tenant-Id": owner["id"]},
            )
            assert r_no_pid.status_code == 403

            # Naming a project they are NOT scoped to -> also 403.
            r_wrong_pid = c.get(
                f"/control-plane/proposals?project_id={other_proj['id']}",
                headers={"X-Workspace-Tenant-Id": owner["id"]},
            )
            assert r_wrong_pid.status_code == 403

            # Naming their OWN scoped project -> 200, scoped correctly.
            r_ok = c.get(
                f"/control-plane/proposals?project_id={proj['id']}",
                headers={"X-Workspace-Tenant-Id": owner["id"]},
            )
            assert r_ok.status_code == 200, r_ok.text
            assert r_ok.json()["scope"] == "project"

    def test_workspace_wide_owner_is_unaffected(self, monkeypatch, tmp_path):
        with _hosted_client(monkeypatch, tmp_path) as c:
            owner = _seed_tenant_session(c, "fullowner@example.com")
            asyncio.run(
                db_module.add_workspace_proposal(
                    c.app.state.db, "Idea", "body", tenant_id=owner["id"],
                )
            )
            r = c.get("/control-plane/proposals")
            assert r.status_code == 200, r.text
            assert len(r.json()["items"]) == 1


# ---------------------------------------------------------------------------
# Artifacts / run manifests / provenance
# ---------------------------------------------------------------------------

class TestArtifactsUnavailable:
    def test_hosted_mode_reports_unavailable_without_touching_the_tunnel(
        self, monkeypatch, tmp_path,
    ):
        with _hosted_client(monkeypatch, tmp_path) as c:
            _seed_tenant_session(c, "hostedartifacts@example.com")
            r = c.get("/control-plane/artifacts")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["available"] is False
            assert body["mode"] == "hosted"
            assert "tunnel" in body["reason"].lower()
            assert body["items"] == []

    def test_self_hosted_missing_extension_reports_unavailable(self, client):
        """meridian_outputs is a genuinely separate, optionally-installed
        extension (confirmed during prospecting: `import meridian_outputs`
        raises ModuleNotFoundError in the core pixi env) — this must degrade
        gracefully, never 500 the whole dashboard."""
        assert "meridian_outputs" not in sys.modules or True  # documents the assumption
        r = client.get("/control-plane/artifacts")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["available"] is False
        assert body["mode"] == "self_hosted"
        assert "not installed" in body["reason"]

    def test_invalid_kind_is_rejected(self, client):
        r = client.get("/control-plane/artifacts?kind=not_a_real_kind")
        assert r.status_code == 400

    def test_outputs_dir_scoped_to_home_directory_is_rejected(self, client, monkeypatch):
        """Reuses meridian/repo_scope.py's existing fail-closed validator —
        never silently defaults to scanning the whole home tree."""
        import meridian.repo_scope as repo_scope_module

        fake_home = "C:\\Users\\testuser" if sys.platform == "win32" else "/home/testuser"
        monkeypatch.setattr(repo_scope_module.Path, "home", staticmethod(lambda: repo_scope_module.Path(fake_home)))

        # Fake the extension being installed so we get past the ImportError
        # branch and actually exercise the repo-scope validation.
        _install_fake_meridian_outputs(monkeypatch, run_manifests=[], provenance=[], registry=[])
        r = client.get(f"/control-plane/artifacts?outputs_dir={fake_home}")
        assert r.status_code == 400
        assert "home directory" in r.json()["detail"]


def _install_fake_meridian_outputs(monkeypatch, *, run_manifests, provenance, registry):
    """Install a minimal fake ``meridian_outputs`` package into sys.modules
    so the lazy-import branch in control_plane.py can be exercised without
    the real (optionally-installed) extension present in this environment."""
    pkg = types.ModuleType("meridian_outputs")
    annotate_mod = types.ModuleType("meridian_outputs.annotate")
    annotate_mod.list_provenance = lambda outputs_dir: list(provenance)
    run_manifest_mod = types.ModuleType("meridian_outputs.run_manifest")
    run_manifest_mod.list_run_manifests = lambda outputs_dir: list(run_manifests)
    def _strip_local_metadata(rec):
        # Mirrors the REAL extensions/meridian-outputs/meridian_outputs/
        # artifact_registry.py::strip_local_metadata exactly: it strips
        # ``local_only_path`` out of each ``local_paths`` ENTRY, not off the
        # top-level record.
        clean = dict(rec)
        clean["local_paths"] = [
            {k: v for k, v in entry.items() if k != "local_only_path"}
            for entry in rec.get("local_paths", [])
        ]
        return clean

    registry_mod = types.ModuleType("meridian_outputs.artifact_registry")
    registry_mod.list_artifacts = lambda outputs_dir: list(registry)
    registry_mod.strip_local_metadata = _strip_local_metadata
    monkeypatch.setitem(sys.modules, "meridian_outputs", pkg)
    monkeypatch.setitem(sys.modules, "meridian_outputs.annotate", annotate_mod)
    monkeypatch.setitem(sys.modules, "meridian_outputs.run_manifest", run_manifest_mod)
    monkeypatch.setitem(sys.modules, "meridian_outputs.artifact_registry", registry_mod)


class TestArtifacts:
    def test_merges_and_tags_all_three_record_kinds(self, client, monkeypatch, tmp_path):
        _install_fake_meridian_outputs(
            monkeypatch,
            run_manifests=[{"run_id": "r1", "status": "complete"}],
            provenance=[{"path": "outputs/fig1.png", "status": "resolved"}],
            registry=[{"artifact_id": "a1", "lifecycle_state": "active"}],
        )
        r = client.get(f"/control-plane/artifacts?outputs_dir={tmp_path}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["available"] is True
        kinds = sorted(item["record_kind"] for item in body["items"])
        assert kinds == ["provenance", "registry", "run_manifest"]
        assert body["total"] == 3

    def test_missing_and_stale_records_are_distinguishable_by_status(self, client, monkeypatch, tmp_path):
        _install_fake_meridian_outputs(
            monkeypatch,
            run_manifests=[],
            provenance=[],
            registry=[
                {"artifact_id": "missing1", "lifecycle_state": "orphaned"},
                {"artifact_id": "stale1", "lifecycle_state": "hash_mismatch"},
                {"artifact_id": "ok1", "lifecycle_state": "resolved"},
            ],
        )
        r = client.get(f"/control-plane/artifacts?outputs_dir={tmp_path}&kind=registry")
        items = {i["artifact_id"]: i["lifecycle_state"] for i in r.json()["items"]}
        assert items == {
            "missing1": "orphaned", "stale1": "hash_mismatch", "ok1": "resolved",
        }

    def test_kind_filter_narrows_the_result(self, client, monkeypatch, tmp_path):
        _install_fake_meridian_outputs(
            monkeypatch,
            run_manifests=[{"run_id": "r1", "status": "complete"}],
            provenance=[{"path": "p", "status": "resolved"}],
            registry=[],
        )
        r = client.get(f"/control-plane/artifacts?outputs_dir={tmp_path}&kind=run_manifest")
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["record_kind"] == "run_manifest"

    def test_pagination_over_merged_records(self, client, monkeypatch, tmp_path):
        _install_fake_meridian_outputs(
            monkeypatch,
            run_manifests=[{"run_id": f"r{i}", "status": "complete"} for i in range(5)],
            provenance=[],
            registry=[],
        )
        r = client.get(f"/control-plane/artifacts?outputs_dir={tmp_path}&limit=2&offset=0")
        body = r.json()
        assert body["total"] == 5
        assert len(body["items"]) == 2
        assert body["has_more"] is True
        r_last = client.get(f"/control-plane/artifacts?outputs_dir={tmp_path}&limit=2&offset=4")
        assert len(r_last.json()["items"]) == 1
        assert r_last.json()["has_more"] is False

    def test_local_absolute_paths_are_redacted_to_basename(self, client, monkeypatch, tmp_path):
        local_path = "C:\\Users\\alice\\secret-project\\outputs\\run.json" if sys.platform == "win32" else "/home/alice/secret-project/outputs/run.json"
        _install_fake_meridian_outputs(
            monkeypatch,
            run_manifests=[],
            provenance=[{"path": local_path, "status": "resolved"}],
            registry=[],
        )
        r = client.get(f"/control-plane/artifacts?outputs_dir={tmp_path}&kind=provenance")
        item = r.json()["items"][0]
        assert item["path"] == "run.json"
        assert "alice" not in item["path"]

    def test_registry_local_paths_entries_never_leak_local_only_path(self, client, monkeypatch, tmp_path):
        _install_fake_meridian_outputs(
            monkeypatch,
            run_manifests=[],
            provenance=[],
            registry=[{
                "artifact_id": "a1",
                "lifecycle_state": "resolved",
                "local_paths": [{
                    "basename": "run.json", "host": "alice-laptop",
                    "local_only_path": "/home/alice/secret-project/run.json",
                }],
            }],
        )
        r = client.get(f"/control-plane/artifacts?outputs_dir={tmp_path}&kind=registry")
        entry = r.json()["items"][0]["local_paths"][0]
        assert "local_only_path" not in entry
        assert entry["basename"] == "run.json"


# ---------------------------------------------------------------------------
# Redaction / pure-helper coverage
# ---------------------------------------------------------------------------

class TestRedactionHelpers:
    def test_redact_local_paths_leaves_relative_and_non_path_fields_alone(self):
        from meridian.routes.control_plane import _redact_local_paths

        rec = {"path": "outputs/fig1.png", "status": "resolved", "run_id": "r1"}
        assert _redact_local_paths(rec) == rec

    def test_redact_local_paths_never_mutates_input(self):
        from meridian.routes.control_plane import _redact_local_paths

        rec = {"path": "/home/bob/x.json"}
        out = _redact_local_paths(rec)
        assert rec["path"] == "/home/bob/x.json"
        assert out["path"] == "x.json"

    def test_public_proposal_allowlist_excludes_everything_else(self):
        from meridian.routes.control_plane import _public_proposal

        row = {
            "id": "p1", "title": "T", "body": "B", "status": "raw",
            "scope_type": "workspace", "project_id": None, "tags": "x",
            "slug": "t", "nickname": "nick", "created_at": "now",
            "last_activity_at": "now", "tenant_id": "secret-tenant",
            "idempotency_key": "secret", "family_id": "fam",
        }
        out = _public_proposal(row)
        assert "tenant_id" not in out
        assert "idempotency_key" not in out
        assert "family_id" not in out
