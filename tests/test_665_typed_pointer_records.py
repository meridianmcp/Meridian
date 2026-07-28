"""Tests for sprint item 4e44139e (665 follow-up) — serialize typed
sprint-item pointers in XML and JSON handoffs.

Covers:

1. meridian.pointers.build_typed_pointer_record — pure typed-record
   builder: resolved/unresolved/planned/stale/archival status, canonical
   metadata (symbol/node_id/zotero_key/finding_id), archival metadata
   (text_quote), malformed/empty input degrades to None.
2. meridian.pointers.build_item_pointer_records — resolve+type a batch of
   stored pointers in one pass.
3. meridian.pointers.assemble_pointer_entries_from_annotated_items — pure
   assembly of the canonical {item_id, provenance, pointers} entries.
4. meridian.handoff._annotate_resolved_pointers — sets pointer_records /
   pointer_provenance alongside the legacy resolved_pointers, from a single
   resolve pass; legacy resolved_pointers stays byte-for-byte unchanged.
5. meridian.handoff._build_pointer_records_clause — the <sprint_item_pointers>
   XML clause.
6. meridian.capability_contract.extract_sprint_item_pointers /
   build_capability_contract — the JSON item_sprint_item_pointers section,
   both the pre-annotated fast path and the self-fetch path.
7. XML/JSON parity — generate_handoff's <sprint_item_pointers> clause carries
   IDENTICAL typed data to capability_contract's item_sprint_item_pointers.
8. Backward compatibility — the legacy compact resolved_pointers / goal
   pointer lines keep rendering unchanged alongside the new typed clause.
"""
from __future__ import annotations

import json as _json

import pytest

from meridian import capability_contract as cc
from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian import pointers as pointers_module


# ---------------------------------------------------------------------------
# Resolver stubs — never touch a network / live Zotero / doc_store.
# ---------------------------------------------------------------------------

async def _stub_node_resolver(element_id):
    if element_id == "el-1":
        return {"element": {"id": "el-1", "kind": "heading", "text": "Intro"},
                "document": {"id": "doc-1", "title": "Thesis"}}
    return None


async def _stub_citation_resolver(ref):
    if ref == "zotero:GOOD":
        return {"zotero_key": "GOOD", "doi": "10.1/x", "title": "A Paper"}
    return None


async def _stub_finding_resolver(fid):
    if fid == "note-1":
        return {"id": "note-1", "title": "Finding: exp run"}
    return None


# ---------------------------------------------------------------------------
# 1. build_typed_pointer_record — pure
# ---------------------------------------------------------------------------

def test_typed_record_range_resolved():
    stored = {"source_type": "code", "id": "ptr-1", "label": "the fix site",
              "targets": [{"uri": "file:a.py",
                           "selector": {"type": "range", "start_line": 1, "end_line": 2},
                           "target_kind": "existing"}]}
    resolved = {"source_type": "code", "targets": [
        {"resolved": True, "selector_type": "range", "uri": "file:a.py",
         "range": {"start_line": 1, "end_line": 2}},
    ]}
    rec = pointers_module.build_typed_pointer_record(stored, resolved)
    assert rec["source_type"] == "code"
    assert rec["id"] == "ptr-1"
    assert rec["label"] == "the fix site"
    t = rec["targets"][0]
    assert t["uri"] == "file:a.py"
    assert t["target_kind"] == "existing"
    assert t["resolved"] is True
    assert t["status"] == "resolved"
    assert t["selector"] == {"type": "range", "start_line": 1, "end_line": 2}
    assert "canonical" not in t
    assert "archival" not in t


def test_typed_record_unresolved_symbol_has_reason():
    stored = {"source_type": "code", "targets": [
        {"uri": "file:x.py", "selector": {"type": "symbol", "qualified_name": "x.missing"},
         "target_kind": "existing"},
    ]}
    resolved = {"source_type": "code", "targets": [
        {"resolved": False, "selector_type": "symbol", "uri": "file:x.py",
         "qualified_name": "x.missing", "reason": "no matching symbol in graph snapshot"},
    ]}
    rec = pointers_module.build_typed_pointer_record(stored, resolved)
    t = rec["targets"][0]
    assert t["resolved"] is False
    assert t["status"] == "unresolved"
    assert t["reason"] == "no matching symbol in graph snapshot"


def test_typed_record_planned_new_status_is_planned_even_when_unresolved():
    stored = {"source_type": "code", "targets": [
        {"uri": "file:new_module.py", "selector": {"type": "range", "start_line": 1, "end_line": 1},
         "target_kind": "planned_new"},
    ]}
    # Not resolved at all (no resolved dict passed) — planned still wins over
    # "unresolved" as the explicit status.
    rec = pointers_module.build_typed_pointer_record(stored, None)
    t = rec["targets"][0]
    assert t["target_kind"] == "planned_new"
    assert t["status"] == "planned"
    assert t["resolved"] is False


def test_typed_record_text_quote_stale_when_drift():
    stored = {"source_type": "web", "targets": [
        {"uri": "https://x/a", "selector": {"type": "text_quote", "exact": "the cited passage"},
         "target_kind": "existing"},
    ]}
    resolved = {"source_type": "web", "targets": [
        {"resolved": True, "selector_type": "text_quote", "uri": "https://x/a",
         "exact": "the cited passage", "found": False, "drift": True},
    ]}
    rec = pointers_module.build_typed_pointer_record(stored, resolved)
    t = rec["targets"][0]
    assert t["status"] == "stale"
    assert t["resolved"] is True


def test_typed_record_text_quote_archival_when_archived_url_present():
    stored = {"source_type": "web", "targets": [
        {"uri": "https://x/a", "selector": {"type": "text_quote", "exact": "the cited passage"},
         "target_kind": "existing"},
    ]}
    resolved = {"source_type": "web", "targets": [
        {"resolved": True, "selector_type": "text_quote", "uri": "https://x/a",
         "exact": "the cited passage", "found": True, "drift": False,
         "archived_url": "https://web.archive.org/web/2/https://x/a",
         "archived_at": "2026-01-01T00:00:00Z"},
    ]}
    rec = pointers_module.build_typed_pointer_record(stored, resolved)
    t = rec["targets"][0]
    assert t["status"] == "archival"
    assert t["archival"] == {
        "archived_url": "https://web.archive.org/web/2/https://x/a",
        "archived_at": "2026-01-01T00:00:00Z",
        "drift": False,
    }
    assert "canonical" not in t


def test_typed_record_zotero_canonical_metadata():
    stored = {"source_type": "citation", "targets": [
        {"uri": "zotero:GOOD", "selector": {"type": "zotero_key", "key": "GOOD"},
         "target_kind": "existing"},
    ]}
    resolved = {"source_type": "citation", "targets": [
        {"resolved": True, "selector_type": "zotero_key", "uri": "zotero:GOOD", "key": "GOOD",
         "item": {"zotero_key": "GOOD", "doi": "10.1/x", "title": "A Paper"}},
    ]}
    rec = pointers_module.build_typed_pointer_record(stored, resolved)
    t = rec["targets"][0]
    assert t["status"] == "resolved"
    assert t["canonical"] == {"item": {"zotero_key": "GOOD", "doi": "10.1/x", "title": "A Paper"}}


def test_typed_record_node_id_canonical_metadata():
    stored = {"source_type": "docs", "targets": [
        {"uri": "doc:1", "selector": {"type": "node_id", "id": "el-1"}, "target_kind": "existing"},
    ]}
    resolved = {"source_type": "docs", "targets": [
        {"resolved": True, "selector_type": "node_id", "uri": "doc:1", "id": "el-1",
         "element": {"id": "el-1", "kind": "heading", "text": "Intro"},
         "document": {"id": "doc-1", "title": "Thesis"}},
    ]}
    rec = pointers_module.build_typed_pointer_record(stored, resolved)
    t = rec["targets"][0]
    assert t["canonical"]["element"]["text"] == "Intro"
    assert t["canonical"]["document"]["title"] == "Thesis"


def test_typed_record_finding_id_canonical_metadata():
    stored = {"source_type": "experiment", "targets": [
        {"uri": "finding:note-1", "selector": {"type": "finding_id", "id": "note-1"},
         "target_kind": "existing"},
    ]}
    resolved = {"source_type": "experiment", "targets": [
        {"resolved": True, "selector_type": "finding_id", "uri": "finding:note-1", "id": "note-1",
         "artifact": {"id": "note-1", "title": "Finding: exp run"}},
    ]}
    rec = pointers_module.build_typed_pointer_record(stored, resolved)
    t = rec["targets"][0]
    assert t["canonical"] == {"artifact": {"id": "note-1", "title": "Finding: exp run"}}


def test_typed_record_none_for_malformed_or_empty():
    assert pointers_module.build_typed_pointer_record(None) is None
    assert pointers_module.build_typed_pointer_record({"source_type": "code", "targets": []}) is None
    assert pointers_module.build_typed_pointer_record({"source_type": "code", "targets": "nope"}) is None
    # Malformed individual target is skipped, not fatal.
    stored = {"source_type": "code", "targets": ["not-a-dict"]}
    assert pointers_module.build_typed_pointer_record(stored, None) is None


def test_typed_record_sub_resolved_and_narrowed_range_surfaced():
    stored = {"source_type": "code", "targets": [
        {"uri": "a.py",
         "selector": {"type": "symbol", "qualified_name": "found.symbol",
                       "subSelector": {"type": "range", "start_line": 12, "end_line": 15}},
         "target_kind": "existing"},
    ]}
    resolved = {"source_type": "code", "targets": [
        {"resolved": True, "selector_type": "symbol", "uri": "a.py",
         "qualified_name": "found.symbol", "file": "found.py",
         "subResolved": {"resolved": True, "selector_type": "range",
                          "uri": "a.py", "range": {"start_line": 12, "end_line": 15}},
         "narrowed_range": {"start_line": 12, "end_line": 15}},
    ]}
    rec = pointers_module.build_typed_pointer_record(stored, resolved)
    t = rec["targets"][0]
    assert t["sub_resolved"] == {"resolved": True}
    assert t["narrowed_range"] == {"start_line": 12, "end_line": 15}


# ---------------------------------------------------------------------------
# 2. build_item_pointer_records — resolve+type a batch (guarded seams only)
# ---------------------------------------------------------------------------

async def test_build_item_pointer_records_mixed_hit_and_miss():
    stored_pointers = [
        {"source_type": "code", "id": "p1", "targets": [
            {"uri": "file:a.py", "selector": {"type": "range", "start_line": 1, "end_line": 2},
             "target_kind": "existing"},
        ]},
        {"source_type": "docs", "id": "p2", "targets": [
            {"uri": "doc:1", "selector": {"type": "node_id", "id": "nope"},
             "target_kind": "existing"},
        ]},
    ]
    records = await pointers_module.build_item_pointer_records(
        None, "pid", stored_pointers, node_resolver=_stub_node_resolver,
    )
    assert len(records) == 2
    by_id = {r["id"]: r for r in records}
    assert by_id["p1"]["targets"][0]["status"] == "resolved"
    assert by_id["p2"]["targets"][0]["status"] == "unresolved"


# ---------------------------------------------------------------------------
# 3. assemble_pointer_entries_from_annotated_items — pure
# ---------------------------------------------------------------------------

def test_assemble_skips_items_with_no_records_and_no_required_provenance():
    items = [
        {"id": "a"},
        {"id": "b", "pointer_provenance": {"required": False, "bypassed": False, "satisfied": True}},
    ]
    assert pointers_module.assemble_pointer_entries_from_annotated_items(items) == []


def test_assemble_includes_required_provenance_even_with_zero_pointers():
    items = [
        {"id": "needs-evidence",
         "pointer_provenance": {"required": True, "bypassed": False, "satisfied": False}},
    ]
    out = pointers_module.assemble_pointer_entries_from_annotated_items(items)
    assert out == [{
        "item_id": "needs-evidence",
        "provenance": {"required": True, "bypassed": False, "satisfied": False},
    }]


def test_assemble_deterministic_order_and_includes_pointers():
    items = [
        {"id": "zzz", "pointer_records": [{"source_type": "code", "targets": [
            {"uri": "b.py", "selector": {"type": "range", "start_line": 1, "end_line": 1},
             "target_kind": "existing", "resolved": True, "status": "resolved"}]}]},
        {"id": "aaa", "pointer_records": [{"source_type": "code", "targets": [
            {"uri": "a.py", "selector": {"type": "range", "start_line": 1, "end_line": 1},
             "target_kind": "existing", "resolved": True, "status": "resolved"}]}]},
    ]
    out = pointers_module.assemble_pointer_entries_from_annotated_items(items)
    assert [e["item_id"] for e in out] == ["aaa", "zzz"]
    assert "pointers" in out[0]


# ---------------------------------------------------------------------------
# 4. handoff._annotate_resolved_pointers — DB-backed
# ---------------------------------------------------------------------------

async def test_annotate_sets_typed_records_alongside_legacy_compact(db):
    p = await db_module.create_project(db, "665-annotate")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Patch the parser")
    await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "code",
        [{"uri": "file:meridian/parser.py",
          "selector": {"type": "range", "start_line": 42, "end_line": 58}}],
        label="parser entry",
    )
    items = [{"id": item["id"], "title": "Patch the parser"}]
    out = await handoff_module._annotate_resolved_pointers(db, p["id"], items)
    it = out[0]
    # Legacy compact field — byte-for-byte unchanged shape.
    assert it["resolved_pointers"][0]["targets"] == ["file:meridian/parser.py:42-58"]
    # New typed field.
    rec = it["pointer_records"][0]
    assert rec["label"] == "parser entry"
    t = rec["targets"][0]
    assert t["target_kind"] == "existing"
    assert t["status"] == "resolved"
    assert t["selector"]["type"] == "range"
    # Provenance — item declared no touches_resources, so not required.
    assert it["pointer_provenance"] == {"required": False, "bypassed": False, "satisfied": True}


async def test_annotate_provenance_required_true_with_no_evidence(db):
    p = await db_module.create_project(db, "665-provenance-required")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Needs prospecting",
        touches_resources=["file:meridian/some_real_module.py"],
    )
    items = [item]
    out = await handoff_module._annotate_resolved_pointers(db, p["id"], items)
    it = out[0]
    assert "pointer_records" not in it
    assert it["pointer_provenance"] == {"required": True, "bypassed": False, "satisfied": False}


async def test_annotate_provenance_bypassed(db):
    p = await db_module.create_project(db, "665-provenance-bypassed")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Bypassed item",
        touches_resources=["file:meridian/some_real_module.py"],
        prospect_bypass=True,
    )
    out = await handoff_module._annotate_resolved_pointers(db, p["id"], [item])
    it = out[0]
    assert it["pointer_provenance"] == {"required": False, "bypassed": True, "satisfied": True}


# ---------------------------------------------------------------------------
# 5. handoff._build_pointer_records_clause
# ---------------------------------------------------------------------------

def test_build_pointer_records_clause_empty_for_no_data():
    assert handoff_module._build_pointer_records_clause([]) == ""
    assert handoff_module._build_pointer_records_clause([{"id": "x"}]) == ""


def test_build_pointer_records_clause_embeds_canonical_json():
    items = [{
        "id": "item-1",
        "pointer_records": [{"source_type": "code", "targets": [
            {"uri": "a.py", "selector": {"type": "range", "start_line": 1, "end_line": 1},
             "target_kind": "existing", "resolved": True, "status": "resolved"},
        ]}],
        "pointer_provenance": {"required": False, "bypassed": False, "satisfied": True},
    }]
    out = handoff_module._build_pointer_records_clause(items)
    assert out.startswith("\n<sprint_item_pointers>")
    assert out.endswith("</sprint_item_pointers>")
    body = out[len("\n<sprint_item_pointers>"):-len("</sprint_item_pointers>")]
    embedded = _json.loads(body)
    assert embedded == pointers_module.assemble_pointer_entries_from_annotated_items(items)


# ---------------------------------------------------------------------------
# 6/7. XML/JSON parity + capability_contract extraction
# ---------------------------------------------------------------------------

async def test_generate_handoff_xml_and_contract_carry_identical_pointer_records(db, tmp_path):
    project = await db_module.create_project(db, "665-pointers-parity")
    item = await db_module.add_sprint_item(
        db, project["id"], "v1", "Fix the migration guard",
        touches_resources=["file:meridian/db/migrations.py"],
    )
    await db_module.add_sprint_item_pointer(
        db, project["id"], item["id"], "code",
        [{"uri": "file:meridian/db/migrations.py",
          "selector": {"type": "range", "start_line": 100, "end_line": 120}}],
        label="guard site",
    )
    # A second item that declares resources but has NO durable pointer yet —
    # provenance_required must still show up explicitly with zero pointers.
    await db_module.add_sprint_item(
        db, project["id"], "v1", "Needs prospecting still",
        touches_resources=["file:meridian/some_other_module.py"],
    )

    _, content, _ = await handoff_module.generate_handoff(
        db, project["id"], str(tmp_path), mode="goal", skip_ai_summary=True,
    )
    assert "<sprint_item_pointers>" in content
    start = content.index("<sprint_item_pointers>") + len("<sprint_item_pointers>")
    end = content.index("</sprint_item_pointers>")
    xml_typed = _json.loads(content[start:end])
    assert len(xml_typed) == 2

    by_id = {e["item_id"]: e for e in xml_typed}
    assert by_id[item["id"]]["pointers"][0]["targets"][0]["status"] == "resolved"
    assert by_id[item["id"]]["provenance"]["satisfied"] is True
    other = [e for e in xml_typed if e["item_id"] != item["id"]][0]
    assert other["provenance"] == {"required": True, "bypassed": False, "satisfied": False}
    assert "pointers" not in other

    contract = await cc.build_capability_contract(db, project["id"])
    assert contract["item_sprint_item_pointers"] == xml_typed


async def test_build_capability_contract_item_sprint_item_pointers_empty_project(db):
    project = await db_module.create_project(db, "665-pointers-empty-contract")
    contract = await cc.build_capability_contract(db, project["id"])
    assert contract["item_sprint_item_pointers"] == []


async def test_extract_sprint_item_pointers_self_fetch_matches_pre_annotated(db):
    """Whether the caller pre-annotates items (via _annotate_resolved_pointers)
    or passes raw items and lets extract_sprint_item_pointers self-fetch +
    resolve, the two paths produce identical typed output for the same data.
    """
    project = await db_module.create_project(db, "665-pointers-self-fetch")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Self-fetch parity")
    await db_module.add_sprint_item_pointer(
        db, project["id"], item["id"], "code",
        [{"uri": "file:a.py",
          "selector": {"type": "range", "start_line": 1, "end_line": 3}}],
    )

    raw_items = [dict(item)]
    self_fetched = await cc.extract_sprint_item_pointers(db, project["id"], raw_items)

    annotated_items = [dict(item)]
    await handoff_module._annotate_resolved_pointers(db, project["id"], annotated_items)
    pre_annotated = await cc.extract_sprint_item_pointers(db, project["id"], annotated_items)

    assert self_fetched == pre_annotated
    assert self_fetched[0]["item_id"] == item["id"]
    assert self_fetched[0]["pointers"][0]["targets"][0]["status"] == "resolved"


async def test_build_capability_contract_accepts_explicit_items_override_for_pointers(db):
    project = await db_module.create_project(db, "665-pointers-explicit-items")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Explicit override")
    await db_module.add_sprint_item_pointer(
        db, project["id"], item["id"], "code",
        [{"uri": "file:a.py", "selector": {"type": "range", "start_line": 1, "end_line": 3}}],
    )
    items = [dict(item)]
    contract = await cc.build_capability_contract(db, project["id"], items=items)
    expected = await cc.extract_sprint_item_pointers(db, project["id"], items)
    assert contract["item_sprint_item_pointers"] == expected


# ---------------------------------------------------------------------------
# 8. Backward compatibility — legacy compact rendering unaffected
# ---------------------------------------------------------------------------

async def test_legacy_compact_pointer_rendering_unaffected_by_typed_clause(db, tmp_path):
    p = await db_module.create_project(db, "665-legacy-compat")
    await db_module.set_goal(db, p["id"], "ship typed pointers")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Fix the migration guard")
    await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "code",
        [{"uri": "file:meridian/db/migrations.py",
          "selector": {"type": "range", "start_line": 100, "end_line": 120}}],
        label="guard site",
    )

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True,
    )
    # Legacy markdown rendering (36fea6ca) still present, unchanged.
    assert "Resolved pointers:" in content
    assert "meridian/db/migrations.py:100-120" in content
    assert "guard site" in content
    # New typed XML clause is ALSO present (full mode still renders the /goal
    # block, which now carries the typed clause too).
    assert "<sprint_item_pointers>" in content


async def test_goal_only_mode_still_renders_legacy_pointer_lines_and_typed_clause(db, tmp_path):
    p = await db_module.create_project(db, "665-goal-only-compat")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Fix the migration guard")
    await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "code",
        [{"uri": "file:meridian/db/migrations.py",
          "selector": {"type": "range", "start_line": 100, "end_line": 120}}],
    )
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), mode="goal", skip_ai_summary=True,
    )
    # 682005f4 legacy compact goal-line rendering still present.
    assert "meridian/db/migrations.py:100-120" in content
    # New typed clause also present.
    assert "<sprint_item_pointers>" in content
