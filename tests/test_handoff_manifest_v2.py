"""Tests for acf6f51a — canonical XML HandoffManifest with body-bound tokens
and board-snapshot identity.

Scope: the manifest is a machine envelope a receiver can check WITHOUT
re-parsing prose — board_revision, project/tenant identity, generated_at,
selected/closure item ids, the full item id/status/depends_on/resources
list, and the wave plan. It is never independently authored: every field is
data generate_handoff already computed for its own human-readable render.

Covers:
  - compute_board_revision / verify_board_revision determinism + drift
    detection.
  - build_handoff_manifest field shape, item-count bounding (never silent —
    items_truncated/items_total), board_revision computed over the FULL
    (unbounded) item list.
  - serialize_handoff_manifest_xml: deterministic, escapes untrusted text,
    fails closed (HandoffManifestTooLarge) instead of truncating a
    token-bound body.
  - mint_manifest_bound_token round-trips through the EXISTING
    verify_handoff_token(presented_body=...) body-hash mechanism — no new
    verification path.
  - generate_handoff(mode="goal", emit_manifest=True) end-to-end: the
    manifest is embedded in the rendered /goal text BEFORE the goal token is
    minted, so the token's body_hash covers it too; emit_manifest=False
    (the default) is byte-for-byte unaffected.
"""
from __future__ import annotations

import re

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


def _extract_manifest_block(text: str) -> str | None:
    m = re.search(r"<handoff_manifest\b.*?</handoff_manifest>", text, re.DOTALL)
    return m.group(0) if m else None


def _extract_token(text: str) -> str | None:
    m = re.search(r"<goal_token>([^<]+)</goal_token>", text)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# compute_board_revision / verify_board_revision
# ---------------------------------------------------------------------------


def test_compute_board_revision_is_deterministic_and_order_independent():
    a = [{"id": "1", "status": "todo", "depends_on": None}, {"id": "2", "status": "todo", "depends_on": "1"}]
    b = [{"id": "2", "status": "todo", "depends_on": "1"}, {"id": "1", "status": "todo", "depends_on": None}]
    assert handoff_module.compute_board_revision(a) == handoff_module.compute_board_revision(b)


def test_compute_board_revision_changes_on_status_change():
    a = [{"id": "1", "status": "todo", "depends_on": None}]
    b = [{"id": "1", "status": "in_progress", "depends_on": None}]
    assert handoff_module.compute_board_revision(a) != handoff_module.compute_board_revision(b)


def test_verify_board_revision_true_when_unchanged_false_on_drift():
    items = [{"id": "1", "status": "todo", "depends_on": None}]
    rev = handoff_module.compute_board_revision(items)
    assert handoff_module.verify_board_revision(items, rev) is True

    drifted = [{"id": "1", "status": "done", "depends_on": None}]
    assert handoff_module.verify_board_revision(drifted, rev) is False


# ---------------------------------------------------------------------------
# build_handoff_manifest
# ---------------------------------------------------------------------------


def _mk_items(n: int) -> list[dict]:
    return [
        {
            "id": f"item-{i}",
            "title": f"do thing {i}",
            "status": "todo",
            "depends_on": None,
            "wave": None,
            "touches_resources": None,
        }
        for i in range(n)
    ]


def test_build_handoff_manifest_basic_shape():
    items = _mk_items(3)
    manifest = handoff_module.build_handoff_manifest(
        handoff_mode="goal",
        project_id="proj-1",
        items=items,
        project_name="Proj One",
        sprint_version="v1",
        origin_identity={"tenant_id": "t1"},
        selected_item_ids=["item-0"],
        closure_item_ids=["item-0"],
        waves=[[{"id": "item-0"}], [{"id": "item-1"}, {"id": "item-2"}]],
    )
    assert manifest["schema_version"] == handoff_module._MANIFEST_SCHEMA_VERSION
    assert manifest["handoff_mode"] == "goal"
    assert manifest["project_id"] == "proj-1"
    assert manifest["project_name"] == "Proj One"
    assert manifest["sprint_version"] == "v1"
    assert manifest["origin_identity"] == {"tenant_id": "t1"}
    assert manifest["selected_item_ids"] == ["item-0"]
    assert manifest["closure_item_ids"] == ["item-0"]
    assert manifest["items_total"] == 3
    assert manifest["items_truncated"] is False
    assert [it["id"] for it in manifest["items"]] == ["item-0", "item-1", "item-2"]
    assert manifest["board_revision"] == handoff_module.compute_board_revision(items)


def test_build_handoff_manifest_bounds_items_and_reports_truncation():
    items = _mk_items(5)
    manifest = handoff_module.build_handoff_manifest(
        handoff_mode="goal", project_id="proj-1", items=items, max_items=2,
    )
    assert manifest["items_truncated"] is True
    assert manifest["items_total"] == 5
    assert len(manifest["items"]) == 2
    # board_revision is computed over the FULL list, not the truncated view.
    assert manifest["board_revision"] == handoff_module.compute_board_revision(items)


def test_build_handoff_manifest_carries_resources_from_touches_resources():
    items = [{
        "id": "item-0", "title": "t", "status": "todo", "depends_on": None,
        "wave": None, "touches_resources": '["file:a.py", "symbol:a.py::foo"]',
    }]
    manifest = handoff_module.build_handoff_manifest(
        handoff_mode="goal", project_id="proj-1", items=items,
    )
    assert manifest["items"][0]["resources"] == ["file:a.py", "symbol:a.py::foo"]


# ---------------------------------------------------------------------------
# serialize_handoff_manifest_xml
# ---------------------------------------------------------------------------


def test_serialize_handoff_manifest_xml_is_deterministic():
    manifest = handoff_module.build_handoff_manifest(
        handoff_mode="goal", project_id="proj-1", items=_mk_items(2),
        generated_at="2026-08-10T00:00:00+00:00",
    )
    x1 = handoff_module.serialize_handoff_manifest_xml(manifest)
    x2 = handoff_module.serialize_handoff_manifest_xml(manifest)
    assert x1 == x2
    assert x1.startswith("<handoff_manifest ")
    assert x1.endswith("</handoff_manifest>")
    assert 'board_revision="' in x1
    assert "<item id=\"item-0\"" in x1


def test_serialize_handoff_manifest_xml_escapes_untrusted_item_titles():
    items = [{
        "id": "item-0", "title": "<script>alert(1)</script> & \"quote\"",
        "status": "todo", "depends_on": None, "wave": None,
        "touches_resources": None,
    }]
    manifest = handoff_module.build_handoff_manifest(
        handoff_mode="goal", project_id="proj-1", items=items,
    )
    xml = handoff_module.serialize_handoff_manifest_xml(manifest)
    assert "<script>" not in xml
    assert "&lt;script&gt;" in xml
    assert "&amp;" in xml


def test_serialize_handoff_manifest_xml_raises_instead_of_truncating(monkeypatch):
    monkeypatch.setattr(handoff_module, "_MANIFEST_MAX_BYTES", 64)
    manifest = handoff_module.build_handoff_manifest(
        handoff_mode="goal", project_id="proj-huge", items=_mk_items(10),
    )
    with pytest.raises(handoff_module.HandoffManifestTooLarge) as excinfo:
        handoff_module.serialize_handoff_manifest_xml(manifest)
    assert excinfo.value.project_id == "proj-huge"
    assert excinfo.value.code == "MANIFEST_TOO_LARGE"


# ---------------------------------------------------------------------------
# mint_manifest_bound_token — reuses the existing body-hash mechanism
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mint_manifest_bound_token_verifies_against_manifest_body(db):
    p = await db_module.create_project(db, "manifest-token-test")
    manifest = handoff_module.build_handoff_manifest(
        handoff_mode="goal", project_id=p["id"], items=_mk_items(1),
    )
    xml = handoff_module.serialize_handoff_manifest_xml(manifest)
    token = await handoff_module.mint_manifest_bound_token(db, p["id"], xml)

    ok = await handoff_module.verify_handoff_token(db, token, p["id"], body=xml)
    assert ok["valid"] is True
    assert ok["reason"] == "ok"


@pytest.mark.asyncio
async def test_mint_manifest_bound_token_rejects_tampered_body(db):
    p = await db_module.create_project(db, "manifest-token-tamper-test")
    manifest = handoff_module.build_handoff_manifest(
        handoff_mode="goal", project_id=p["id"], items=_mk_items(1),
    )
    xml = handoff_module.serialize_handoff_manifest_xml(manifest)
    token = await handoff_module.mint_manifest_bound_token(db, p["id"], xml)

    tampered = xml.replace("item-0", "item-999")
    bad = await handoff_module.verify_handoff_token(
        db, token, p["id"], body=tampered
    )
    assert bad["valid"] is False
    assert bad["reason"] == "body_mismatch"


# ---------------------------------------------------------------------------
# generate_handoff(mode="goal", emit_manifest=...) end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_goal_mode_emits_manifest_when_opted_in(db, tmp_path):
    p = await db_module.create_project(db, "goal-manifest-e2e")
    await db_module.set_goal(db, p["id"], "ship it", sprint="v1")
    it = await db_module.add_sprint_item(db, p["id"], "v1", "do the thing")

    _path, content, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), mode="goal", skip_ai_summary=True,
        emit_manifest=True,
    )

    block = _extract_manifest_block(content)
    assert block is not None, "emit_manifest=True must embed a <handoff_manifest> block"
    assert f'project_id="{p["id"]}"' in block
    assert it["id"] in block

    # The manifest is embedded BEFORE the token is minted, so the token's
    # body_hash covers it — the same trust mechanism the rest of the /goal
    # block already relies on (efaa918a).
    token = _extract_token(content)
    assert token is not None
    verified = await handoff_module.verify_handoff_token(
        db, token, p["id"],
        body=handoff_module.strip_goal_token_banner(content),
    )
    assert verified["valid"] is True, (
        f"manifest-carrying /goal body must still verify against its own "
        f"token: {verified}"
    )


@pytest.mark.asyncio
async def test_goal_mode_omits_manifest_by_default(db, tmp_path):
    p = await db_module.create_project(db, "goal-manifest-default-off")
    await db_module.set_goal(db, p["id"], "ship it", sprint="v1")
    await db_module.add_sprint_item(db, p["id"], "v1", "do the thing")

    _path, content, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), mode="goal", skip_ai_summary=True,
    )
    assert "<handoff_manifest" not in content, (
        "emit_manifest defaults to False — no existing caller's output should change"
    )


@pytest.mark.asyncio
async def test_goal_mode_manifest_board_revision_matches_live_items(db, tmp_path):
    p = await db_module.create_project(db, "goal-manifest-revision")
    await db_module.set_goal(db, p["id"], "ship it", sprint="v1")
    await db_module.add_sprint_item(db, p["id"], "v1", "item a")
    await db_module.add_sprint_item(db, p["id"], "v1", "item b", force=True)

    _path, content, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), mode="goal", skip_ai_summary=True,
        emit_manifest=True,
    )
    block = _extract_manifest_block(content)
    m = re.search(r'board_revision="([^"]+)"', block)
    assert m is not None
    rendered_revision = m.group(1)

    live_items = await db_module.get_sprint_items(
        db, p["id"], include_human=False, include_deferred=False, version="v1",
    )
    pending = [it for it in live_items if it.get("status") in ("todo", "pending")]
    assert handoff_module.verify_board_revision(pending, rendered_revision) is True
