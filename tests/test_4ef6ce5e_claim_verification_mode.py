"""4ef6ce5e — claim_verification_mode workspace setting + PostToolUse
verification hook.

Motivated by a 2026-07-19 incident where an executor session narrated a
claim as landed when the live sprint board showed otherwise. claim_sprint_item's
own write path is already race-safe (fa3e3331); the gap this closes is one
layer up — does downstream narration match live DB reality, independent of
what any single tool call claims happened (d0f05438's "lost/malformed
dispatch response" class in particular). See meridian/claim_verify.py's
module docstring for the full design writeup.

Covers:
1. DB layer — get_workspace_settings/update_workspace_settings roundtrip,
   default, clear-to-off, case-insensitivity, invalid-value rejection,
   malformed-row tolerance.
2. GET /projects/{id}/sprint-items/{item_id} — the live single-item route the
   hook's HTTP fetch hits.
3. meridian.claim_verify pure functions — _tool_suffix, parse_hook_payload,
   response_is_declined, narrated_status, verify_transition,
   evaluate_hook_payload (skip/ok/mismatch cases, including the "tool_response
   missing entirely" dispatch-failure class).
4. fetch_sprint_item_live — mocked HTTP, no real network.
5. seed_claim_verification_hook — registers/updates/removes via the 273287cb
   custom_hooks infra, mirroring orphan_reaper's test shape.
6. main() — CLI entry point, exit codes (0 = no issue, 2 = mismatch).
7. handoff._write_sprint_guard_hooks wiring — claim_verification_mode drives
   whether/how the hook gets (re)written; 'off' writes nothing.
"""
from __future__ import annotations

import asyncio
import io
import json
import sys

import pytest

from meridian import claim_verify
from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian import hook_paths


# ---------------------------------------------------------------------------
# 1. DB layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_settings_claim_verification_mode_defaults_off(db):
    ws = await db_module.get_workspace_settings(db)
    assert ws["claim_verification_mode"] == "off"


@pytest.mark.asyncio
async def test_update_workspace_settings_claim_verification_mode_roundtrip(db):
    ws = await db_module.update_workspace_settings(db, claim_verification_mode="advisory")
    assert ws["claim_verification_mode"] == "advisory"
    ws2 = await db_module.get_workspace_settings(db)
    assert ws2["claim_verification_mode"] == "advisory"

    ws3 = await db_module.update_workspace_settings(db, claim_verification_mode="strict")
    assert ws3["claim_verification_mode"] == "strict"


@pytest.mark.asyncio
async def test_update_workspace_settings_claim_verification_mode_clear(db):
    await db_module.update_workspace_settings(db, claim_verification_mode="strict")
    ws = await db_module.update_workspace_settings(db, claim_verification_mode="")
    assert ws["claim_verification_mode"] == "off"


@pytest.mark.asyncio
async def test_update_workspace_settings_claim_verification_mode_case_insensitive(db):
    ws = await db_module.update_workspace_settings(db, claim_verification_mode="STRICT")
    assert ws["claim_verification_mode"] == "strict"


@pytest.mark.asyncio
async def test_update_workspace_settings_claim_verification_mode_invalid_raises(db):
    with pytest.raises(ValueError):
        await db_module.update_workspace_settings(db, claim_verification_mode="paranoid")
    # A rejected write must not have partially applied.
    ws = await db_module.get_workspace_settings(db)
    assert ws["claim_verification_mode"] == "off"


@pytest.mark.asyncio
async def test_workspace_settings_claim_verification_mode_malformed_row_falls_back_to_off(db):
    await db_module.update_workspace_settings(db)  # ensure the row exists
    settings_key = db_module.workspace._ws_settings_key(None)
    await db.execute(
        "UPDATE workspace_settings SET claim_verification_mode = ? WHERE id = ?",
        ("garbage", settings_key),
    )
    await db.commit()
    ws = await db_module.get_workspace_settings(db)
    assert ws["claim_verification_mode"] == "off"


# ---------------------------------------------------------------------------
# 2. GET /projects/{id}/sprint-items/{item_id}
# ---------------------------------------------------------------------------


def test_get_sprint_item_route_returns_live_item(client):
    project = client.post("/projects", json={"name": "cvm-route-test"}).json()
    pid = project["id"]
    item = client.post(
        f"/projects/{pid}/sprint-items",
        json={"version": "v1", "title": "do the thing"},
    ).json()

    r = client.get(f"/projects/{pid}/sprint-items/{item['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == item["id"]
    assert body["status"] == item["status"]


def test_get_sprint_item_route_reflects_claim(client):
    project = client.post("/projects", json={"name": "cvm-route-claim"}).json()
    pid = project["id"]
    item = client.post(
        f"/projects/{pid}/sprint-items",
        json={"version": "v1", "title": "claim me"},
    ).json()
    # Claim via the DB layer directly (avoids coupling this route test to the
    # MCP claim_sprint_item handler's own request/response shape).
    asyncio.run(db_module.claim_sprint_item(client.app.state.db, pid, item["id"], actor="sess-1"))

    r = client.get(f"/projects/{pid}/sprint-items/{item['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "in_progress"
    assert body["actor"] == "sess-1"


def test_get_sprint_item_route_404_unknown_item(client):
    project = client.post("/projects", json={"name": "cvm-route-404"}).json()
    r = client.get(f"/projects/{project['id']}/sprint-items/does-not-exist")
    assert r.status_code == 404


def test_get_sprint_item_route_404_wrong_project(client):
    p1 = client.post("/projects", json={"name": "cvm-route-p1"}).json()
    p2 = client.post("/projects", json={"name": "cvm-route-p2"}).json()
    item = client.post(
        f"/projects/{p1['id']}/sprint-items",
        json={"version": "v1", "title": "p1 item"},
    ).json()
    r = client.get(f"/projects/{p2['id']}/sprint-items/{item['id']}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 3. meridian.claim_verify pure functions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,expected", [
    ("claim_sprint_item", "claim_sprint_item"),
    ("mcp__meridian__claim_sprint_item", "claim_sprint_item"),
    ("Meridian: claim_sprint_item", "claim_sprint_item"),
    ("complete_sprint_item", "complete_sprint_item"),
    ("mcp__meridian__complete_sprint_item", "complete_sprint_item"),
    ("Meridian: complete_sprint_item", "complete_sprint_item"),
    ("get_sprint_items", None),
    ("", None),
    (None, None),
])
def test_tool_suffix(name, expected):
    assert claim_verify._tool_suffix(name) == expected


def test_parse_hook_payload_valid_json():
    payload = claim_verify.parse_hook_payload('{"tool_name": "claim_sprint_item"}')
    assert payload == {"tool_name": "claim_sprint_item"}


def test_parse_hook_payload_empty_returns_empty_dict():
    assert claim_verify.parse_hook_payload("") == {}
    assert claim_verify.parse_hook_payload("   ") == {}


def test_parse_hook_payload_malformed_json_returns_empty_dict():
    assert claim_verify.parse_hook_payload("not json") == {}


def test_parse_hook_payload_non_dict_json_returns_empty_dict():
    assert claim_verify.parse_hook_payload("[1, 2, 3]") == {}


@pytest.mark.parametrize("response,expected", [
    ({"error": "PROTECTED"}, True),
    ({"blocked": True, "error": "DEFERRED"}, True),
    ({"status": "already_claimed"}, True),
    ({"id": "x", "status": "in_progress"}, False),
    (None, False),
    ("not a dict", False),
])
def test_response_is_declined(response, expected):
    assert claim_verify.response_is_declined(response) is expected


def test_narrated_status_extracts_status():
    assert claim_verify.narrated_status({"status": "in_progress"}) == "in_progress"


def test_narrated_status_missing_or_malformed_returns_none():
    assert claim_verify.narrated_status(None) is None
    assert claim_verify.narrated_status({}) is None
    assert claim_verify.narrated_status({"status": 123}) is None


def test_verify_transition_ok_when_status_matches():
    result = claim_verify.verify_transition({"status": "in_progress"}, "in_progress")
    assert result["ok"] is True


def test_verify_transition_mismatch_when_status_differs():
    result = claim_verify.verify_transition({"status": "pending"}, "in_progress")
    assert result["ok"] is False
    assert "pending" in result["reason"]


def test_verify_transition_mismatch_when_live_item_none():
    result = claim_verify.verify_transition(None, "in_progress")
    assert result["ok"] is False


def test_verify_transition_ok_when_actor_matches():
    result = claim_verify.verify_transition(
        {"status": "in_progress", "actor": "sess-1"}, "in_progress", expected_actor="sess-1"
    )
    assert result["ok"] is True


def test_verify_transition_mismatch_when_actor_differs():
    result = claim_verify.verify_transition(
        {"status": "in_progress", "actor": "sess-2"}, "in_progress", expected_actor="sess-1"
    )
    assert result["ok"] is False
    assert "sess-2" in result["reason"]


def test_verify_transition_no_actor_check_when_live_actor_blank():
    # A live row with no recorded actor at all can't be compared -- must not
    # be treated as a mismatch just because expected_actor was supplied.
    result = claim_verify.verify_transition(
        {"status": "in_progress", "actor": None}, "in_progress", expected_actor="sess-1"
    )
    assert result["ok"] is True


def test_evaluate_hook_payload_skips_unwatched_tool():
    payload = {"tool_name": "get_sprint_items", "tool_input": {}, "tool_response": {}}
    result = claim_verify.evaluate_hook_payload(payload, "http://x")
    assert result["action"] == "skip"


def test_evaluate_hook_payload_skips_declined_response():
    payload = {
        "tool_name": "claim_sprint_item",
        "tool_input": {"project_id": "p1", "item_id": "i1"},
        "tool_response": {"error": "PROTECTED"},
    }
    result = claim_verify.evaluate_hook_payload(payload, "http://x")
    assert result["action"] == "skip"


def test_evaluate_hook_payload_skips_missing_ids():
    payload = {
        "tool_name": "claim_sprint_item",
        "tool_input": {},
        "tool_response": {"status": "in_progress"},
    }
    result = claim_verify.evaluate_hook_payload(payload, "http://x")
    assert result["action"] == "skip"


def test_evaluate_hook_payload_ok_when_live_state_matches():
    payload = {
        "tool_name": "claim_sprint_item",
        "tool_input": {"project_id": "p1", "item_id": "i1", "session_id": "sess-1"},
        "tool_response": {"id": "i1", "status": "in_progress", "actor": "sess-1"},
    }

    def fetch(base_url, project_id, item_id):
        return {"status": "in_progress", "actor": "sess-1"}

    result = claim_verify.evaluate_hook_payload(payload, "http://x", fetch_fn=fetch)
    assert result["action"] == "ok"


def test_evaluate_hook_payload_mismatch_when_live_state_diverges():
    # The narration-vs-reality gap this feature exists to catch: tool_response
    # claims success but the live re-fetch shows it never actually landed.
    payload = {
        "tool_name": "claim_sprint_item",
        "tool_input": {"project_id": "p1", "item_id": "i1", "session_id": "sess-1"},
        "tool_response": {"id": "i1", "status": "in_progress", "actor": "sess-1"},
    }

    def fetch(base_url, project_id, item_id):
        return {"status": "pending", "actor": None}

    result = claim_verify.evaluate_hook_payload(payload, "http://x", fetch_fn=fetch)
    assert result["action"] == "mismatch"
    assert result["item_id"] == "i1"
    assert result["narrated_status"] == "in_progress"


def test_evaluate_hook_payload_mismatch_when_tool_response_missing():
    # d0f05438's class -- tool_response is empty/None (a lost/malformed
    # dispatch response) but the item still never transitioned live. This is
    # exactly the case the whole feature exists to catch: no signal from the
    # response to lean on, so the live re-check is the only source of truth.
    payload = {
        "tool_name": "claim_sprint_item",
        "tool_input": {"project_id": "p1", "item_id": "i1"},
        "tool_response": None,
    }

    def fetch(base_url, project_id, item_id):
        return {"status": "pending"}

    result = claim_verify.evaluate_hook_payload(payload, "http://x", fetch_fn=fetch)
    assert result["action"] == "mismatch"
    assert result["narrated_status"] is None


def test_evaluate_hook_payload_watches_complete_sprint_item_too():
    payload = {
        "tool_name": "mcp__meridian__complete_sprint_item",
        "tool_input": {"project_id": "p1", "item_id": "i1"},
        "tool_response": {"id": "i1", "status": "done"},
    }

    def fetch(base_url, project_id, item_id):
        return {"status": "in_progress"}

    result = claim_verify.evaluate_hook_payload(payload, "http://x", fetch_fn=fetch)
    assert result["action"] == "mismatch"


def test_evaluate_hook_payload_never_raises_on_internal_error():
    def fetch(*a, **k):
        raise RuntimeError("boom")

    payload = {
        "tool_name": "claim_sprint_item",
        "tool_input": {"project_id": "p1", "item_id": "i1"},
        "tool_response": {"id": "i1", "status": "in_progress"},
    }
    result = claim_verify.evaluate_hook_payload(payload, "http://x", fetch_fn=fetch)
    assert result["action"] == "skip"


# ---------------------------------------------------------------------------
# 4. fetch_sprint_item_live -- mocked HTTP, no real network
# ---------------------------------------------------------------------------


def test_fetch_sprint_item_live_parses_json(monkeypatch):
    body = json.dumps({"id": "i1", "status": "in_progress"}).encode("utf-8")

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return body

    def _fake_urlopen(url, timeout=5.0):
        assert "/sprint-items/i1" in url
        return _FakeResp()

    monkeypatch.setattr(claim_verify.urllib.request, "urlopen", _fake_urlopen)
    item = claim_verify.fetch_sprint_item_live("http://localhost:7878", "p1", "i1")
    assert item == {"id": "i1", "status": "in_progress"}


def test_fetch_sprint_item_live_network_error_returns_none(monkeypatch):
    def _fake_urlopen(url, timeout=5.0):
        raise OSError("connection refused")

    monkeypatch.setattr(claim_verify.urllib.request, "urlopen", _fake_urlopen)
    assert claim_verify.fetch_sprint_item_live("http://localhost:7878", "p1", "i1") is None


def test_fetch_sprint_item_live_malformed_json_returns_none(monkeypatch):
    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"not json"

    monkeypatch.setattr(
        claim_verify.urllib.request, "urlopen", lambda url, timeout=5.0: _FakeResp()
    )
    assert claim_verify.fetch_sprint_item_live("http://localhost:7878", "p1", "i1") is None


# ---------------------------------------------------------------------------
# 5. seed_claim_verification_hook -- registers via 273287cb custom_hooks infra
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_claim_verification_hook_off_is_noop(db):
    p = await db_module.create_project(db, "cvm-seed-off")
    result = await claim_verify.seed_claim_verification_hook(db, p["id"], "off")
    assert result is None
    hooks = await db_module.get_custom_hooks(db, p["id"])
    assert hooks == []


@pytest.mark.asyncio
async def test_seed_claim_verification_hook_advisory_registers_non_blocking(db):
    p = await db_module.create_project(db, "cvm-seed-advisory")
    hook = await claim_verify.seed_claim_verification_hook(
        db, p["id"], "advisory", url="http://localhost:9999"
    )
    assert hook["slug"] == claim_verify.HOOK_NAME
    assert hook["event"] == "PostToolUse"
    assert bool(hook["blocking"]) is False
    assert hook["enabled"]
    assert p["id"] in hook["script_sh"]
    assert "http://localhost:9999" in hook["script_sh"]
    assert "meridian.claim_verify" in hook["script_sh"]
    assert "meridian.claim_verify" in hook["script_ps1"]

    all_hooks = await db_module.get_custom_hooks(db, p["id"])
    assert len(all_hooks) == 1


@pytest.mark.asyncio
async def test_seed_claim_verification_hook_strict_registers_blocking(db):
    p = await db_module.create_project(db, "cvm-seed-strict")
    hook = await claim_verify.seed_claim_verification_hook(db, p["id"], "strict")
    assert bool(hook["blocking"]) is True


@pytest.mark.asyncio
async def test_seed_claim_verification_hook_is_idempotent_and_refreshes_mode(db):
    p = await db_module.create_project(db, "cvm-seed-idempotent")
    first = await claim_verify.seed_claim_verification_hook(db, p["id"], "advisory")
    second = await claim_verify.seed_claim_verification_hook(db, p["id"], "strict")
    assert first["id"] == second["id"]
    assert bool(second["blocking"]) is True

    all_hooks = await db_module.get_custom_hooks(db, p["id"])
    assert len(all_hooks) == 1


@pytest.mark.asyncio
async def test_seed_claim_verification_hook_off_removes_existing_registration(db):
    p = await db_module.create_project(db, "cvm-seed-off-removes")
    await claim_verify.seed_claim_verification_hook(db, p["id"], "strict")
    assert len(await db_module.get_custom_hooks(db, p["id"])) == 1

    result = await claim_verify.seed_claim_verification_hook(db, p["id"], "off")
    assert result is None
    assert await db_module.get_custom_hooks(db, p["id"]) == []


@pytest.mark.asyncio
async def test_seed_claim_verification_hook_invalid_mode_raises(db):
    p = await db_module.create_project(db, "cvm-seed-invalid")
    with pytest.raises(ValueError):
        await claim_verify.seed_claim_verification_hook(db, p["id"], "paranoid")


@pytest.mark.asyncio
async def test_seed_claim_verification_hook_never_uses_reserved_sprint_guard_slug(db):
    p = await db_module.create_project(db, "cvm-seed-slug")
    hook = await claim_verify.seed_claim_verification_hook(db, p["id"], "strict")
    assert hook["slug"] != "sprint_guard"


# ---------------------------------------------------------------------------
# 6. main() -- CLI entry point, exit codes
# ---------------------------------------------------------------------------


def test_main_returns_zero_for_unwatched_tool(monkeypatch):
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"tool_name": "get_sprint_items"}))
    )
    rc = claim_verify.main(["--project-id", "p1", "--url", "http://x"])
    assert rc == 0


def test_main_returns_two_on_mismatch(monkeypatch):
    payload = {
        "tool_name": "claim_sprint_item",
        "tool_input": {"project_id": "p1", "item_id": "i1"},
        "tool_response": {"id": "i1", "status": "in_progress"},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(
        claim_verify, "fetch_sprint_item_live", lambda *a, **k: {"status": "pending"}
    )
    rc = claim_verify.main(["--project-id", "p1", "--url", "http://x"])
    assert rc == 2


def test_main_returns_zero_on_match(monkeypatch):
    payload = {
        "tool_name": "claim_sprint_item",
        "tool_input": {"project_id": "p1", "item_id": "i1"},
        "tool_response": {"id": "i1", "status": "in_progress"},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(
        claim_verify, "fetch_sprint_item_live", lambda *a, **k: {"status": "in_progress"}
    )
    rc = claim_verify.main(["--project-id", "p1", "--url", "http://x"])
    assert rc == 0


def test_main_handles_empty_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = claim_verify.main(["--project-id", "p1", "--url", "http://x"])
    assert rc == 0


# ---------------------------------------------------------------------------
# 7. handoff._write_sprint_guard_hooks wiring
# ---------------------------------------------------------------------------


def test_write_sprint_guard_hooks_registers_advisory_claim_verification_hook(tmp_path):
    async def _run():
        db = await db_module.init_db(":memory:")
        await db_module.update_workspace_settings(db, claim_verification_mode="advisory")
        await handoff_module._write_sprint_guard_hooks(db, "proj-cvm-1", root=tmp_path)

    asyncio.run(_run())
    hooks_dir = tmp_path / ".claude" / "hooks"
    sh = (hooks_dir / "claim_verification_guard.sh").read_text(encoding="utf-8")
    assert "proj-cvm-1" in sh
    assert "meridian.claim_verify" in sh
    # advisory -> non-blocking -> the exit-2-downgrade wrapper is present.
    assert 'exit "$_meridian_hook_rc"' in sh


def test_write_sprint_guard_hooks_registers_strict_blocking_claim_verification_hook(tmp_path):
    async def _run():
        db = await db_module.init_db(":memory:")
        await db_module.update_workspace_settings(db, claim_verification_mode="strict")
        await handoff_module._write_sprint_guard_hooks(db, "proj-cvm-2", root=tmp_path)

    asyncio.run(_run())
    hooks_dir = tmp_path / ".claude" / "hooks"
    sh = (hooks_dir / "claim_verification_guard.sh").read_text(encoding="utf-8")
    # strict -> blocking -> written byte-for-byte, no downgrade wrapper.
    assert 'exit "$_meridian_hook_rc"' not in sh
    assert "meridian.claim_verify" in sh


def test_write_sprint_guard_hooks_off_mode_writes_no_claim_verification_hook(tmp_path):
    async def _run():
        db = await db_module.init_db(":memory:")
        # claim_verification_mode left at its 'off' default.
        await handoff_module._write_sprint_guard_hooks(db, "proj-cvm-3", root=tmp_path)

    asyncio.run(_run())
    hooks_dir = tmp_path / ".claude" / "hooks"
    assert not (hooks_dir / "claim_verification_guard.sh").exists()
    # sprint_guard itself is unaffected either way.
    assert (hooks_dir / "sprint_guard.sh").exists()


def test_write_sprint_guard_hooks_switching_to_off_removes_previously_written_hook(tmp_path):
    # Regression guard: turning the mode back to 'off' after it was
    # previously 'strict'/'advisory', then re-running the handoff hook-writer
    # (the only place seed_claim_verification_hook is invoked), must remove
    # the STALE custom_hooks row so a later handoff doesn't keep re-emitting
    # a hook nobody wants enforced anymore. Merely flipping the workspace
    # setting does NOT retroactively touch already-registered hooks -- that
    # only happens the next time _write_sprint_guard_hooks runs.
    async def _run():
        db = await db_module.init_db(":memory:")
        await db_module.update_workspace_settings(db, claim_verification_mode="strict")
        await handoff_module._write_sprint_guard_hooks(db, "proj-cvm-4", root=tmp_path)
        hooks_after_strict = await db_module.get_custom_hooks(db, "proj-cvm-4")
        assert any(h["slug"] == claim_verify.HOOK_NAME for h in hooks_after_strict)

        await db_module.update_workspace_settings(db, claim_verification_mode="off")
        await handoff_module._write_sprint_guard_hooks(db, "proj-cvm-4", root=tmp_path)
        return await db_module.get_custom_hooks(db, "proj-cvm-4")

    hooks = asyncio.run(_run())
    assert not any(h["slug"] == claim_verify.HOOK_NAME for h in hooks)


# ---------------------------------------------------------------------------
# 8. _write_sprint_guard_hooks production path (root=None) -- e5eec33b
#
# repo_path resolution now goes through hook_paths.resolve_repo_root_for_handoff
# instead of a bare Path(repo_path).exists() check, so a repo_path recorded
# from a WSL/Linux session still resolves when the handoff runs on native
# Windows. This is the same production (root=None) codepath the
# claim-verification hook seeding above (section 5/7) rides on, so a broken
# repo_path resolution here would silently strand claim-verification-hook
# projects with no repo-local hooks at all.
# ---------------------------------------------------------------------------


def test_write_sprint_guard_hooks_production_path_seeds_claim_verification_hook(tmp_path, monkeypatch):
    # Simulate "production" by lifting the PYTEST_CURRENT_TEST short-circuit
    # (see tests/test_sprint_guard.py for the established pattern).
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    (tmp_path / ".claude").mkdir()

    async def _run():
        db = await db_module.init_db(":memory:")
        p = await db_module.create_project(db, "cvm-prod-path")
        await db_module.update_workspace_settings(db, claim_verification_mode="strict")
        await db_module.set_executor_config(db, p["id"], {"repo_path": str(tmp_path)})
        await handoff_module._write_sprint_guard_hooks(db, p["id"])
        return p["id"]

    project_id = asyncio.run(_run())
    hooks_dir = tmp_path / ".claude" / "hooks"
    assert (hooks_dir / "sprint_guard.sh").exists()
    sh = (hooks_dir / "claim_verification_guard.sh").read_text(encoding="utf-8")
    assert project_id in sh


@pytest.mark.skipif(sys.platform != "win32", reason="WSL drive-letter round-trip is Windows-specific")
def test_write_sprint_guard_hooks_production_path_normalizes_wsl_repo_path(tmp_path, monkeypatch):
    """A repo_path recorded from a WSL/Linux session (/mnt/c/...) must still
    resolve when this handoff runs on native Windows -- previously a bare
    Path(repo_path) check meant this project's hooks (including the
    claim-verification guard) silently never got written."""
    (tmp_path / ".claude").mkdir()
    drive = str(tmp_path)[0].lower()
    rest = str(tmp_path)[2:].replace("\\", "/").strip("/")
    wsl_style = f"/mnt/{drive}/{rest}"
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    async def _run():
        db = await db_module.init_db(":memory:")
        p = await db_module.create_project(db, "cvm-prod-wsl")
        await db_module.set_executor_config(db, p["id"], {"repo_path": wsl_style})
        await handoff_module._write_sprint_guard_hooks(db, p["id"])

    asyncio.run(_run())
    assert (tmp_path / ".claude" / "hooks" / "sprint_guard.sh").exists()


def test_write_sprint_guard_hooks_production_path_skips_when_repo_path_unresolvable(tmp_path, monkeypatch):
    # A repo_path that resolves to nowhere real (no .claude dir) must skip
    # the write entirely -- hook_paths.resolve_repo_root_for_handoff returns
    # None, matching the pre-existing "no repo of its own" contract.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    bogus = tmp_path / "nope"

    async def _run():
        db = await db_module.init_db(":memory:")
        p = await db_module.create_project(db, "cvm-prod-bogus")
        await db_module.set_executor_config(db, p["id"], {"repo_path": str(bogus)})
        await handoff_module._write_sprint_guard_hooks(db, p["id"])

    asyncio.run(_run())
    assert not bogus.exists()


def test_resolve_repo_root_for_handoff_used_directly_matches_write_sprint_guard_hooks_contract(tmp_path):
    # hook_paths.resolve_repo_root_for_handoff is the exact function
    # _write_sprint_guard_hooks now delegates to for its root=None path --
    # assert its contract directly (valid repo -> Path, no .claude -> None).
    (tmp_path / ".claude").mkdir()
    assert hook_paths.resolve_repo_root_for_handoff(str(tmp_path)) == tmp_path

    no_claude = tmp_path.parent / "no-claude-here"
    no_claude.mkdir(exist_ok=True)
    assert hook_paths.resolve_repo_root_for_handoff(str(no_claude)) is None
