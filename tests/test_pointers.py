"""Coverage for the GENERIC POINTER PRIMITIVE (2976e168).

Exercises, end to end and in isolation:

* the pointer MODEL + validation (valid pointers round-trip; malformed selectors
  are rejected; subSelector nesting validates recursively),
* JSON serialize/deserialize of the ``targets`` column,
* the DB helpers (add/get/delete round-trip on ``sprint_item_pointers``),
* the ONE resolver — each selector.type (range/symbol/node_id/zotero_key)
  dispatches correctly with STUBBED code-graph / doc_store / Zotero seams (no
  network, no live Zotero); unresolvable → guarded ``{resolved: False}``; range
  returns as-is; a subSelector narrows the outer resolution,
* the three MCP tools through the real ``_dispatch_mcp_tool`` path.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import pointers as pointers_module
from meridian.pointers import (
    PointerValidationError,
    validate_pointer,
    serialize_targets,
    deserialize_targets,
    resolve_pointer,
    repair_pointer_set,
)


# ---------------------------------------------------------------------------
# Model + validation (pure)
# ---------------------------------------------------------------------------

def test_validate_pointer_range_round_trips():
    ptr = {
        "source_type": "code",
        "targets": [
            {"uri": "meridian/server.py",
             "selector": {"type": "range", "start_line": 10, "start_char": 0,
                          "end_line": 20, "end_char": 5}},
        ],
        "label": "the lifespan",
    }
    normalized = validate_pointer(ptr)
    assert normalized["source_type"] == "code"
    assert normalized["label"] == "the lifespan"
    sel = normalized["targets"][0]["selector"]
    assert sel["type"] == "range"
    assert sel["start_line"] == 10 and sel["end_line"] == 20


def test_validate_pointer_symbol_node_zotero_variants():
    ptr = validate_pointer({
        "source_type": "code",
        "targets": [
            {"uri": "a.py", "selector": {"type": "symbol", "qualified_name": "a.b.c"}},
            {"uri": "doc:1", "selector": {"type": "node_id", "id": "el-1"}},
            {"uri": "zotero:", "selector": {"type": "zotero_key", "key": "ABCD1234"}},
        ],
    })
    kinds = [t["selector"]["type"] for t in ptr["targets"]]
    assert kinds == ["symbol", "node_id", "zotero_key"]
    assert ptr["targets"][0]["selector"]["qualified_name"] == "a.b.c"
    assert "label" not in ptr  # omitted label stays absent


def test_validate_pointer_subselector_nesting():
    ptr = validate_pointer({
        "source_type": "code",
        "targets": [{
            "uri": "a.py",
            "selector": {
                "type": "symbol", "qualified_name": "a.b.func",
                "subSelector": {"type": "range", "start_line": 3, "end_line": 4},
            },
        }],
    })
    sel = ptr["targets"][0]["selector"]
    assert sel["subSelector"]["type"] == "range"
    assert sel["subSelector"]["start_line"] == 3


def test_validate_pointer_target_level_subselector_folds_into_selector():
    """A subSelector placed as a peer of selector (W3C shape) folds in."""
    ptr = validate_pointer({
        "source_type": "code",
        "targets": [{
            "uri": "a.py",
            "selector": {"type": "symbol", "qualified_name": "a.b.func"},
            "subSelector": {"type": "range", "start_line": 1, "end_line": 2},
        }],
    })
    assert ptr["targets"][0]["selector"]["subSelector"]["type"] == "range"


@pytest.mark.parametrize("bad", [
    {"source_type": "", "targets": [{"uri": "a", "selector": {"type": "range", "start_line": 1, "end_line": 2}}]},
    {"source_type": "code", "targets": []},
    {"source_type": "code", "targets": [{"uri": "a", "selector": {"type": "bogus"}}]},
    {"source_type": "code", "targets": [{"uri": "", "selector": {"type": "range", "start_line": 1, "end_line": 2}}]},
    {"source_type": "code", "targets": [{"uri": "a", "selector": {"type": "range", "start_line": "x", "end_line": 2}}]},
    {"source_type": "code", "targets": [{"uri": "a", "selector": {"type": "symbol"}}]},
    {"source_type": "code", "targets": [{"uri": "a", "selector": {"type": "node_id", "id": ""}}]},
    {"source_type": "code", "targets": [{"uri": "a", "selector": {"type": "zotero_key"}}]},
    {"source_type": "code", "targets": [{"uri": "a", "selector": {"type": "symbol", "qualified_name": "x", "subSelector": {"type": "nope"}}}]},
    {"source_type": "code", "targets": "not-a-list"},
    "not-an-object",
])
def test_validate_pointer_rejects_malformed(bad):
    with pytest.raises(PointerValidationError):
        validate_pointer(bad)


def test_validate_pointer_range_rejects_bool_as_int():
    """A bool is not a valid line number even though bool is an int subclass."""
    with pytest.raises(PointerValidationError):
        validate_pointer({
            "source_type": "code",
            "targets": [{"uri": "a", "selector": {"type": "range", "start_line": True, "end_line": 2}}],
        })


# ---------------------------------------------------------------------------
# 300a063d — target_kind: existing | planned_new
# ---------------------------------------------------------------------------

def test_target_kind_omitted_defaults_to_existing_unchecked():
    """Backward compat: a target with no target_kind key at all normalizes to
    'existing' in the returned shape but is NEVER filesystem-checked — a fake
    placeholder path like 'a.py' (the shape every pre-300a063d pointer/test
    uses) must keep validating exactly as before."""
    ptr = validate_pointer({
        "source_type": "code",
        "targets": [{"uri": "a.py", "selector": {"type": "range",
                     "start_line": 1, "end_line": 2}}],
    })
    assert ptr["targets"][0]["target_kind"] == "existing"


def test_target_kind_existing_real_path_passes(tmp_path):
    """target_kind='existing' explicitly declared on a REAL path validates fine,
    and the checker actually ran (proven by the missing-path counterpart below)."""
    real_file = tmp_path / "real_module.py"
    real_file.write_text("# real file\n")
    ptr = validate_pointer({
        "source_type": "code",
        "targets": [{"uri": str(real_file), "target_kind": "existing",
                     "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    })
    assert ptr["targets"][0]["target_kind"] == "existing"
    assert ptr["targets"][0]["uri"] == str(real_file)


def test_target_kind_existing_missing_path_rejected(tmp_path):
    """target_kind='existing' explicitly declared on a path that does NOT exist
    is rejected — this is the core gap 300a063d closes: a planned-new-file item
    can no longer masquerade as verified, existing-code prospecting."""
    missing = tmp_path / "does_not_exist.py"
    with pytest.raises(PointerValidationError, match="target_kind='existing'"):
        validate_pointer({
            "source_type": "code",
            "targets": [{"uri": str(missing), "target_kind": "existing",
                         "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
        })


def test_target_kind_planned_new_missing_path_allowed(tmp_path):
    """target_kind='planned_new' explicitly allows a nonexistent path — the file
    hasn't been created yet, and that's the whole point of declaring it planned."""
    missing = tmp_path / "not_created_yet.py"
    ptr = validate_pointer({
        "source_type": "code",
        "targets": [{"uri": str(missing), "target_kind": "planned_new",
                     "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    })
    assert ptr["targets"][0]["target_kind"] == "planned_new"
    assert ptr["targets"][0]["uri"] == str(missing)


def test_target_kind_invalid_value_rejected():
    with pytest.raises(PointerValidationError, match="target_kind"):
        validate_pointer({
            "source_type": "code",
            "targets": [{"uri": "a.py", "target_kind": "bogus",
                         "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
        })


def test_target_kind_existing_skips_check_for_non_local_uri_schemes():
    """target_kind='existing' on a non-local-path uri (zotero:/doc:/finding:/a URL)
    is NOT filesystem-checked — those schemes have their own existence semantics,
    resolved elsewhere (resolve_pointer), not a local disk check."""
    for uri, selector in [
        ("zotero:", {"type": "zotero_key", "key": "ABCD1234"}),
        ("doc:1", {"type": "node_id", "id": "el-1"}),
        ("finding:xyz", {"type": "finding_id", "id": "note-1"}),
        ("https://example.com/a", {"type": "text_quote", "exact": "x"}),
    ]:
        ptr = validate_pointer({
            "source_type": "x",
            "targets": [{"uri": uri, "target_kind": "existing", "selector": selector}],
        })
        assert ptr["targets"][0]["target_kind"] == "existing"


def test_target_kind_existing_uses_injectable_path_exists_checker():
    """path_exists is an injectable seam (same pattern as symbol_resolver /
    node_resolver / citation_resolver): tests can stub it instead of touching
    the real filesystem."""
    ptr = {
        "source_type": "code",
        "targets": [{"uri": "some/fake/path.py", "target_kind": "existing",
                     "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    }
    ok = validate_pointer(ptr, path_exists=lambda _uri: True)
    assert ok["targets"][0]["target_kind"] == "existing"
    with pytest.raises(PointerValidationError):
        validate_pointer(ptr, path_exists=lambda _uri: False)


# ---------------------------------------------------------------------------
# ba539706 — canonical local URI/path normalization contract
#
# normalize_local_uri_candidates is the ONE shared normalization both
# validate_pointer's write-time target_kind='existing' check and
# verify_target_readiness's completion-time gate go through, so a pointer
# recorded under one valid path spelling (file:// URI, WSL /mnt/<drive>,
# forward vs. backslash separators, a UNC host) isn't reported "missing"
# purely because a later check ran under a different one. Pure string
# transforms — no real filesystem/OS dependency, so these unit tests run
# identically on every CI platform.
# ---------------------------------------------------------------------------

def test_normalize_local_uri_candidates_empty_uri_returns_empty_list():
    assert pointers_module.normalize_local_uri_candidates("") == []


def test_normalize_local_uri_candidates_relative_path_passthrough():
    """A plain relative path (no drive letter, no /mnt/ prefix, no file://
    scheme) isn't touched beyond separator variants — no normalization is
    invented for it, matching prior (pre-ba539706) behavior exactly."""
    candidates = pointers_module.normalize_local_uri_candidates("some/fake/path.py")
    assert candidates[0] == "some/fake/path.py"
    assert "some\\fake\\path.py" in candidates


def test_normalize_local_uri_candidates_file_uri_posix():
    candidates = pointers_module.normalize_local_uri_candidates(
        "file:///home/alice/doc.docx"
    )
    assert "file:///home/alice/doc.docx" in candidates
    assert "/home/alice/doc.docx" in candidates


def test_normalize_local_uri_candidates_file_uri_windows_drive():
    candidates = pointers_module.normalize_local_uri_candidates(
        "file:///C:/Users/alice/doc.docx"
    )
    assert "C:/Users/alice/doc.docx" in candidates
    assert "C:\\Users\\alice\\doc.docx" in candidates


def test_normalize_local_uri_candidates_file_uri_unc_host():
    candidates = pointers_module.normalize_local_uri_candidates(
        "file://server/share/doc.docx"
    )
    assert "\\\\server\\share\\doc.docx" in candidates


def test_normalize_local_uri_candidates_file_uri_percent_encoded():
    candidates = pointers_module.normalize_local_uri_candidates(
        "file:///C:/Users/alice%20smith/doc.docx"
    )
    assert "C:/Users/alice smith/doc.docx" in candidates


def test_normalize_local_uri_candidates_windows_drive_to_wsl():
    candidates = pointers_module.normalize_local_uri_candidates(
        "C:\\Users\\alice\\doc.docx"
    )
    assert "/mnt/c/Users/alice/doc.docx" in candidates


def test_normalize_local_uri_candidates_wsl_to_windows_drive():
    candidates = pointers_module.normalize_local_uri_candidates(
        "/mnt/c/Users/alice/doc.docx"
    )
    assert "C:\\Users\\alice\\doc.docx" in candidates


def test_normalize_local_uri_candidates_forward_slash_windows_path():
    """A Windows drive path spelled with forward slashes still yields the
    same WSL candidate as the backslash spelling."""
    candidates = pointers_module.normalize_local_uri_candidates(
        "C:/Users/alice/doc.docx"
    )
    assert "/mnt/c/Users/alice/doc.docx" in candidates


def test_normalize_local_uri_candidates_deduplicates_and_orders_original_first():
    candidates = pointers_module.normalize_local_uri_candidates("plain/path.py")
    assert candidates[0] == "plain/path.py"
    assert len(candidates) == len(set(candidates))


def test_looks_like_local_path_treats_file_uri_as_local():
    """file:// is a local filesystem reference (just URI-spelled) — it must
    NOT be excluded by the generic '://' -> non-local rule, or the existence
    check would be silently skipped instead of actually verified."""
    assert pointers_module._looks_like_local_path("file:///C:/Users/alice/doc.docx") is True
    assert pointers_module._looks_like_local_path("file:///home/alice/doc.docx") is True


def test_looks_like_local_path_still_excludes_remote_and_scheme_uris():
    """Non-file:// remote schemes are unaffected — fail-closed behavior for
    genuinely unavailable remote targets is preserved."""
    assert pointers_module._looks_like_local_path("https://example.com/a") is False
    assert pointers_module._looks_like_local_path("zotero:ABCD1234") is False
    assert pointers_module._looks_like_local_path("doc:1") is False
    assert pointers_module._looks_like_local_path("finding:xyz") is False


def test_target_kind_existing_file_uri_normalizes_against_bare_path_checker():
    """A target_kind='existing' file:// uri validates when the injectable
    path_exists checker only recognizes the bare-path spelling (mirrors a
    checker backed by plain os.path.exists, which never understands file://
    URIs directly)."""
    bare = "C:/Users/alice/doc.docx"
    ptr = {
        "source_type": "docs",
        "targets": [{"uri": "file:///C:/Users/alice/doc.docx", "target_kind": "existing",
                     "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    }
    ok = validate_pointer(ptr, path_exists=lambda uri: uri == bare)
    assert ok["targets"][0]["target_kind"] == "existing"


def test_target_kind_existing_wsl_uri_normalizes_against_windows_style_checker():
    """A pointer recorded under the WSL /mnt/<drive> spelling still validates
    when the injectable checker only recognizes the Windows-drive spelling —
    the two-way normalization the ba539706 contract exists for."""
    windows_style = "C:\\Users\\alice\\doc.docx"
    ptr = {
        "source_type": "docs",
        "targets": [{"uri": "/mnt/c/Users/alice/doc.docx", "target_kind": "existing",
                     "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    }
    ok = validate_pointer(ptr, path_exists=lambda uri: uri == windows_style)
    assert ok["targets"][0]["target_kind"] == "existing"


def test_target_kind_existing_normalization_still_fails_closed_when_genuinely_missing():
    """A path that doesn't exist under ANY normalized spelling still raises —
    normalization only widens which SPELLING is accepted, never what counts
    as present."""
    ptr = {
        "source_type": "docs",
        "targets": [{"uri": "file:///C:/Users/alice/ghost.docx", "target_kind": "existing",
                     "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    }
    with pytest.raises(PointerValidationError, match="target_kind='existing'"):
        validate_pointer(ptr, path_exists=lambda _uri: False)


@pytest.mark.asyncio
async def test_db_pointer_target_kind_existing_missing_path_rejected(db, tmp_path):
    """DB layer: add_sprint_item_pointer rejects an explicit target_kind='existing'
    pointer at a nonexistent path BEFORE any write (mirrors
    test_db_pointer_rejects_malformed_before_write's convention)."""
    p = await db_module.create_project(db, "ptr-kind-bad")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "item")
    missing = tmp_path / "ghost.py"
    with pytest.raises(ValueError):
        await db_module.add_sprint_item_pointer(
            db, p["id"], item["id"], "code",
            [{"uri": str(missing), "target_kind": "existing",
              "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
        )
    assert await db_module.get_sprint_item_pointers(db, item["id"]) == []


@pytest.mark.asyncio
async def test_db_pointer_target_kind_planned_new_missing_path_allowed(db, tmp_path):
    """DB layer: a target_kind='planned_new' pointer at a nonexistent path IS
    persisted — a planned-new-file item is real prospecting evidence too, just
    distinguishable from verified existing-code evidence."""
    p = await db_module.create_project(db, "ptr-kind-planned")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "item")
    missing = tmp_path / "new_module.py"
    stored = await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "code",
        [{"uri": str(missing), "target_kind": "planned_new",
          "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    )
    assert stored["targets"][0]["target_kind"] == "planned_new"
    got = await db_module.get_sprint_item_pointers(db, item["id"])
    assert got[0]["targets"][0]["target_kind"] == "planned_new"


@pytest.mark.asyncio
async def test_db_pointer_target_kind_existing_real_path_allowed(db, tmp_path):
    """DB layer: a target_kind='existing' pointer at a REAL path is persisted."""
    p = await db_module.create_project(db, "ptr-kind-real")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "item")
    real_file = tmp_path / "present.py"
    real_file.write_text("# present\n")
    stored = await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "code",
        [{"uri": str(real_file), "target_kind": "existing",
          "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    )
    assert stored["targets"][0]["target_kind"] == "existing"


# ---------------------------------------------------------------------------
# Serialize / deserialize the JSON targets column
# ---------------------------------------------------------------------------

def test_serialize_deserialize_targets_round_trip():
    targets = [
        {"uri": "a.py", "selector": {"type": "symbol", "qualified_name": "a.b"}},
        {"uri": "b.py", "selector": {"type": "range", "start_line": 1, "end_line": 2}},
    ]
    raw = serialize_targets(targets)
    assert isinstance(raw, str)
    assert deserialize_targets(raw) == targets


def test_deserialize_targets_tolerant_of_garbage():
    assert deserialize_targets(None) == []
    assert deserialize_targets("") == []
    assert deserialize_targets("{not json") == []
    assert deserialize_targets("{}") == []  # decoded but not a list
    already = [{"uri": "a", "selector": {"type": "range", "start_line": 1, "end_line": 1}}]
    assert deserialize_targets(already) == already


# ---------------------------------------------------------------------------
# DB helpers (add/get/delete round-trip)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_db_pointer_add_get_delete_round_trip(db):
    p = await db_module.create_project(db, "ptr-proj")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "some item")

    stored = await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "code",
        [{"uri": "meridian/server.py",
          "selector": {"type": "symbol", "qualified_name": "meridian.server.foo"}}],
        label="the foo",
    )
    assert stored["source_type"] == "code"
    assert stored["label"] == "the foo"
    assert stored["sprint_item_id"] == item["id"]
    # targets deserialized back into a list of dicts.
    assert isinstance(stored["targets"], list)
    assert stored["targets"][0]["selector"]["qualified_name"] == "meridian.server.foo"

    got = await db_module.get_sprint_item_pointers(db, item["id"])
    assert len(got) == 1
    assert got[0]["id"] == stored["id"]
    assert got[0]["targets"] == stored["targets"]

    removed = await db_module.delete_sprint_item_pointer(db, stored["id"])
    assert removed is True
    assert await db_module.get_sprint_item_pointers(db, item["id"]) == []
    # Deleting again is a no-op returning False.
    assert await db_module.delete_sprint_item_pointer(db, stored["id"]) is False


@pytest.mark.asyncio
async def test_db_pointer_rejects_malformed_before_write(db):
    p = await db_module.create_project(db, "ptr-bad")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "item")
    with pytest.raises(ValueError):
        await db_module.add_sprint_item_pointer(
            db, p["id"], item["id"], "code",
            [{"uri": "a", "selector": {"type": "bogus"}}],
        )
    # Nothing was persisted.
    assert await db_module.get_sprint_item_pointers(db, item["id"]) == []


@pytest.mark.asyncio
async def test_db_pointer_multi_target_ordering(db):
    p = await db_module.create_project(db, "ptr-multi")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "item")
    a = await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "code",
        [{"uri": "a", "selector": {"type": "range", "start_line": 1, "end_line": 1}}])
    b = await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "docs",
        [{"uri": "b", "selector": {"type": "node_id", "id": "n1"}}])
    got = await db_module.get_sprint_item_pointers(db, item["id"])
    # Both pointers are returned; get_sprint_item_pointers orders by
    # (created_at, id) — a stable, deterministic order (created_at is
    # second-granularity so same-call inserts tie and fall back to id).
    assert len(got) == 2
    assert {g["id"] for g in got} == {a["id"], b["id"]}
    ids = [g["id"] for g in got]
    assert ids == sorted(ids)  # deterministic id tiebreak within a tied second


# ---------------------------------------------------------------------------
# The resolver — dispatch by selector.type (stubbed seams, never network)
# ---------------------------------------------------------------------------

async def _stub_symbol_resolver(_db, _pid, qn, _lim):
    if qn == "found.symbol":
        return [{"qualified_name": "found.symbol", "file": "found.py", "kind": "function"}]
    return []


async def _stub_node_resolver(element_id):
    if element_id == "el-1":
        return {"element": {"id": "el-1", "kind": "heading", "text": "Intro"},
                "document": {"id": "doc-1", "title": "Thesis"}}
    return None


async def _stub_citation_resolver(ref):
    if ref == "zotero:GOOD":
        return {"zotero_key": "GOOD", "doi": "10.1/x", "title": "A Paper"}
    return None


@pytest.mark.asyncio
async def test_resolve_range_returns_location_as_is():
    ptr = {
        "source_type": "code",
        "targets": [{"uri": "a.py", "selector": {
            "type": "range", "start_line": 5, "start_char": 0,
            "end_line": 9, "end_char": 4}}],
    }
    out = await resolve_pointer(None, ptr, project_id="pid",
                                symbol_resolver=_stub_symbol_resolver,
                                node_resolver=_stub_node_resolver,
                                citation_resolver=_stub_citation_resolver)
    t = out["targets"][0]
    assert t["resolved"] is True
    assert t["selector_type"] == "range"
    assert t["uri"] == "a.py"
    assert t["range"] == {"start_line": 5, "start_char": 0, "end_line": 9, "end_char": 4}


@pytest.mark.asyncio
async def test_resolve_symbol_hits_and_misses():
    ptr = {
        "source_type": "code",
        "targets": [
            {"uri": "a.py", "selector": {"type": "symbol", "qualified_name": "found.symbol"}},
            {"uri": "b.py", "selector": {"type": "symbol", "qualified_name": "missing.symbol"}},
        ],
    }
    out = await resolve_pointer(None, ptr, project_id="pid",
                                symbol_resolver=_stub_symbol_resolver,
                                citation_resolver=_stub_citation_resolver)
    hit, miss = out["targets"]
    assert hit["resolved"] is True and hit["file"] == "found.py"
    assert miss["resolved"] is False
    assert "reason" in miss


@pytest.mark.asyncio
async def test_resolve_node_id_hit_and_miss():
    ptr = {
        "source_type": "docs",
        "targets": [
            {"uri": "doc:1", "selector": {"type": "node_id", "id": "el-1"}},
            {"uri": "doc:1", "selector": {"type": "node_id", "id": "nope"}},
        ],
    }
    out = await resolve_pointer(None, ptr, project_id="pid",
                                symbol_resolver=_stub_symbol_resolver,
                                node_resolver=_stub_node_resolver,
                                citation_resolver=_stub_citation_resolver)
    hit, miss = out["targets"]
    assert hit["resolved"] is True
    assert hit["element"]["text"] == "Intro"
    assert hit["document"]["title"] == "Thesis"
    assert miss["resolved"] is False


@pytest.mark.asyncio
async def test_resolve_node_id_no_store_is_unresolved():
    ptr = {"source_type": "docs",
           "targets": [{"uri": "d", "selector": {"type": "node_id", "id": "el-1"}}]}
    # node_resolver defaults to None → cannot resolve, but never raises.
    out = await resolve_pointer(None, ptr, project_id="pid",
                                symbol_resolver=_stub_symbol_resolver,
                                citation_resolver=_stub_citation_resolver)
    assert out["targets"][0]["resolved"] is False
    assert "no doc_store" in out["targets"][0]["reason"]


@pytest.mark.asyncio
async def test_resolve_zotero_key_hit_and_miss():
    ptr = {
        "source_type": "citation",
        "targets": [
            {"uri": "z", "selector": {"type": "zotero_key", "key": "GOOD"}},
            {"uri": "z", "selector": {"type": "zotero_key", "key": "BAD"}},
        ],
    }
    out = await resolve_pointer(None, ptr, project_id="pid",
                                symbol_resolver=_stub_symbol_resolver,
                                citation_resolver=_stub_citation_resolver)
    hit, miss = out["targets"]
    assert hit["resolved"] is True and hit["item"]["zotero_key"] == "GOOD"
    assert miss["resolved"] is False


@pytest.mark.asyncio
async def test_resolve_subselector_narrows_outer():
    """symbol + range subSelector = 'these lines, within this function'."""
    ptr = {
        "source_type": "code",
        "targets": [{
            "uri": "a.py",
            "selector": {
                "type": "symbol", "qualified_name": "found.symbol",
                "subSelector": {"type": "range", "start_line": 12, "end_line": 15},
            },
        }],
    }
    out = await resolve_pointer(None, ptr, project_id="pid",
                                symbol_resolver=_stub_symbol_resolver,
                                citation_resolver=_stub_citation_resolver)
    t = out["targets"][0]
    assert t["resolved"] is True  # outer symbol resolved
    assert t["subResolved"]["resolved"] is True
    assert t["narrowed_range"] == {"start_line": 12, "end_line": 15}


@pytest.mark.asyncio
async def test_resolve_never_raises_on_malformed_target():
    ptr = {"source_type": "code", "targets": ["not-a-dict",
           {"uri": "a", "selector": "not-a-dict"}]}
    out = await resolve_pointer(None, ptr, project_id="pid",
                                symbol_resolver=_stub_symbol_resolver,
                                citation_resolver=_stub_citation_resolver)
    assert all(t["resolved"] is False for t in out["targets"])


@pytest.mark.asyncio
async def test_resolve_symbol_resolver_exception_is_guarded():
    async def _boom(*_a, **_k):
        raise RuntimeError("graph exploded")
    ptr = {"source_type": "code",
           "targets": [{"uri": "a", "selector": {"type": "symbol", "qualified_name": "x"}}]}
    out = await resolve_pointer(None, ptr, project_id="pid",
                                symbol_resolver=_boom,
                                citation_resolver=_stub_citation_resolver)
    assert out["targets"][0]["resolved"] is False


# ---------------------------------------------------------------------------
# repair_pointer_set (3af86d28) — re-resolution before a corrective handoff
# regeneration trusts a set of "added_pointers" evidence.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repair_pointer_set_range_always_repairs():
    """A range selector IS the location — no lookup, always resolves."""
    ptrs = [{
        "source_type": "code",
        "targets": [{"uri": "a.py", "selector": {"type": "range", "start_line": 1, "end_line": 2}}],
    }]
    out = await repair_pointer_set(None, "pid", ptrs)
    assert out["repaired_count"] == 1
    assert out["unresolved_count"] == 0
    assert out["repaired"][0]["resolution"]["targets"][0]["resolved"] is True


@pytest.mark.asyncio
async def test_repair_pointer_set_symbol_resolves_with_injected_resolver():
    ptrs = [{
        "source_type": "code",
        "targets": [{"uri": "found.py", "selector": {"type": "symbol", "qualified_name": "found.symbol"}}],
    }]
    out = await repair_pointer_set(
        None, "pid", ptrs,
        symbol_resolver=_stub_symbol_resolver,
        citation_resolver=_stub_citation_resolver,
    )
    assert out["repaired_count"] == 1
    assert out["unresolved_count"] == 0


@pytest.mark.asyncio
async def test_repair_pointer_set_symbol_unresolved_without_match():
    """No injected resolver matches -> the default resolver finds nothing ->
    unresolved, with an explicit reason (never silently dropped)."""
    ptrs = [{
        "source_type": "code",
        "targets": [{"uri": "gone.py", "selector": {"type": "symbol", "qualified_name": "vanished.symbol"}}],
    }]
    out = await repair_pointer_set(
        None, "pid", ptrs,
        symbol_resolver=_stub_symbol_resolver,
        citation_resolver=_stub_citation_resolver,
    )
    assert out["repaired_count"] == 0
    assert out["unresolved_count"] == 1
    assert out["unresolved"][0]["pointer"]["source_type"] == "code"
    assert "did not resolve" in out["unresolved"][0]["reason"]


@pytest.mark.asyncio
async def test_repair_pointer_set_malformed_pointer_entry_sorted_unresolved():
    out = await repair_pointer_set(None, "pid", ["not-a-dict", 42])
    assert out["repaired_count"] == 0
    assert out["unresolved_count"] == 2
    assert all("malformed" in u["reason"] for u in out["unresolved"])


@pytest.mark.asyncio
async def test_repair_pointer_set_validation_failure_sorted_unresolved():
    """A structurally invalid pointer (empty targets) never reaches
    resolve_pointer — validate_pointer's own PointerValidationError is
    caught and surfaced with a 'validation failed' reason."""
    out = await repair_pointer_set(None, "pid", [{"source_type": "code", "targets": []}])
    assert out["repaired_count"] == 0
    assert out["unresolved_count"] == 1
    assert "validation failed" in out["unresolved"][0]["reason"]


@pytest.mark.asyncio
async def test_repair_pointer_set_empty_and_none_input():
    assert await repair_pointer_set(None, "pid", []) == {
        "repaired": [], "unresolved": [], "repaired_count": 0, "unresolved_count": 0,
    }
    assert await repair_pointer_set(None, "pid", None) == {
        "repaired": [], "unresolved": [], "repaired_count": 0, "unresolved_count": 0,
    }


@pytest.mark.asyncio
async def test_repair_pointer_set_mixed_batch_counts_both():
    ptrs = [
        {"source_type": "code", "targets": [{"uri": "a.py", "selector": {"type": "range", "start_line": 1, "end_line": 1}}]},
        {"source_type": "code", "targets": [{"uri": "gone.py", "selector": {"type": "symbol", "qualified_name": "vanished"}}]},
    ]
    out = await repair_pointer_set(
        None, "pid", ptrs,
        symbol_resolver=_stub_symbol_resolver,
        citation_resolver=_stub_citation_resolver,
    )
    assert out["repaired_count"] == 1
    assert out["unresolved_count"] == 1


# ---------------------------------------------------------------------------
# The three MCP tools via the real dispatch path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_pointer_tools_add_get_resolve(db, monkeypatch):
    from meridian import server as srv

    p = await db_module.create_project(db, "ptr-mcp")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "wire the primitive")

    # Stub the real code-graph search so the symbol resolves deterministically
    # without indexing a repo.
    async def _fake_search(_db, _pid, query, limit=10):
        return [{"qualified_name": "meridian.server.mcp_tools_doc",
                 "file": "meridian/server.py", "kind": "function"}]
    monkeypatch.setattr(db_module, "search_graph_entities", _fake_search)

    # add
    added = await srv._dispatch_mcp_tool(
        "add_sprint_item_pointer",
        {"project_id": p["id"], "sprint_item_id": item["id"], "source_type": "code",
         "targets": [{"uri": "meridian/server.py",
                      "selector": {"type": "symbol",
                                   "qualified_name": "meridian.server.mcp_tools_doc"}}],
         "label": "doc generator"},
        db, "/tmp",
    )
    assert added["label"] == "doc generator"
    ptr_id = added["id"]

    # get
    listed = await srv._dispatch_mcp_tool(
        "get_sprint_item_pointers",
        {"project_id": p["id"], "sprint_item_id": item["id"]},
        db, "/tmp",
    )
    assert listed["sprint_item_id"] == item["id"]
    assert len(listed["pointers"]) == 1
    assert listed["pointers"][0]["id"] == ptr_id

    # resolve
    resolved = await srv._dispatch_mcp_tool(
        "resolve_sprint_item_pointers",
        {"project_id": p["id"], "sprint_item_id": item["id"]},
        db, "/tmp",
    )
    assert len(resolved["pointers"]) == 1
    target = resolved["pointers"][0]["targets"][0]
    assert target["resolved"] is True
    assert target["file"] == "meridian/server.py"


@pytest.mark.asyncio
async def test_mcp_resolve_pointers_reaches_live_graph_via_tenant(db, monkeypatch):
    """653579c5 regression — resolve_sprint_item_pointers must resolve a symbol
    target via the SAME live tunnel-connected graph prospect_symbol reaches,
    not just the (production-empty) codebase_graph_entities snapshot.

    Before the fix, ``tenant`` was accepted by the handler but never threaded
    into symbol resolution at all, so this scenario (empty snapshot, live
    graph has the answer) always returned {resolved: False} even with an
    active code tunnel for this exact tenant/project.
    """
    import meridian.routes.tunnel as _tunnel_mod
    from meridian import server as srv

    p = await db_module.create_project(db, "ptr-live-graph")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "wire the primitive")

    # The local cached snapshot has NOTHING for this project (matches
    # production reality: nothing ever populates codebase_graph_entities).
    async def _empty_snapshot(_db, _pid, query, limit=10):
        return []
    monkeypatch.setattr(db_module, "search_graph_entities", _empty_snapshot)

    # But a live code tunnel for this tenant resolves the symbol instantly.
    async def _fake_call_tunnel(tid, name, args, **kw):
        if name == "codebase__search_graph":
            return {"content": [{"type": "text", "text":
                '{"results": [{"qualified_name": "meridian.server.mcp_tools_doc", '
                '"file": "meridian/server.py"}]}'}]}
        raise AssertionError(f"unexpected tunnel tool: {name}")
    monkeypatch.setattr(_tunnel_mod, "call_tunnel_tool", _fake_call_tunnel)
    monkeypatch.setattr(_tunnel_mod, "has_active_tunnel", lambda tid: True)

    added = await srv._dispatch_mcp_tool(
        "add_sprint_item_pointer",
        {"project_id": p["id"], "sprint_item_id": item["id"], "source_type": "code",
         "targets": [{"uri": "meridian/server.py",
                      "selector": {"type": "symbol",
                                   "qualified_name": "meridian.server.mcp_tools_doc"}}],
         "label": "doc generator"},
        db, "/tmp",
    )
    assert added["label"] == "doc generator"

    fake_tenant = {"id": "tenant-live-graph"}
    resolved = await srv._dispatch_mcp_tool(
        "resolve_sprint_item_pointers",
        {"project_id": p["id"], "sprint_item_id": item["id"]},
        db, "/tmp",
        tenant=fake_tenant,
    )
    assert len(resolved["pointers"]) == 1
    target = resolved["pointers"][0]["targets"][0]
    assert target["resolved"] is True
    assert target["file"] == "meridian/server.py"


@pytest.mark.asyncio
async def test_mcp_add_pointer_malformed_returns_error(db):
    from meridian import server as srv
    p = await db_module.create_project(db, "ptr-mcp-bad")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "item")
    result = await srv._dispatch_mcp_tool(
        "add_sprint_item_pointer",
        {"project_id": p["id"], "sprint_item_id": item["id"], "source_type": "code",
         "targets": [{"uri": "a", "selector": {"type": "bogus"}}]},
        db, "/tmp",
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_mcp_pointer_tools_by_project_name(db):
    """Project-scoped pointer tools resolve via project_name (no project_id)."""
    from meridian import server as srv
    p = await db_module.create_project(db, "ptr-by-name")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "item")
    added = await srv._dispatch_mcp_tool(
        "add_sprint_item_pointer",
        {"project_name": "ptr-by-name", "sprint_item_id": item["id"],
         "source_type": "code",
         "targets": [{"uri": "a.py", "selector": {"type": "range",
                      "start_line": 1, "end_line": 2}}]},
        db, "/tmp",
    )
    assert added["project_id"] == p["id"]


@pytest.mark.asyncio
async def test_mcp_delete_pointer_removes_and_is_idempotent(db):
    """98c71a42 — the delete MCP tool removes a pointer and is idempotent."""
    from meridian import server as srv
    p = await db_module.create_project(db, "ptr-del")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "item")
    added = await srv._dispatch_mcp_tool(
        "add_sprint_item_pointer",
        {"project_id": p["id"], "sprint_item_id": item["id"], "source_type": "code",
         "targets": [{"uri": "a.py", "selector": {"type": "range",
                      "start_line": 1, "end_line": 2}}]},
        db, "/tmp",
    )
    ptr_id = added["id"]

    # delete the real pointer -> deleted True, and the item has no pointers left
    deleted = await srv._dispatch_mcp_tool(
        "delete_sprint_item_pointer", {"pointer_id": ptr_id}, db, "/tmp",
    )
    assert deleted == {"pointer_id": ptr_id, "deleted": True}
    listed = await srv._dispatch_mcp_tool(
        "get_sprint_item_pointers",
        {"project_id": p["id"], "sprint_item_id": item["id"]},
        db, "/tmp",
    )
    assert listed["pointers"] == []

    # deleting again is idempotent (not an error) -> deleted False
    again = await srv._dispatch_mcp_tool(
        "delete_sprint_item_pointer", {"pointer_id": ptr_id}, db, "/tmp",
    )
    assert again == {"pointer_id": ptr_id, "deleted": False}

    # a missing pointer_id is a clean error, not a crash
    err = await srv._dispatch_mcp_tool(
        "delete_sprint_item_pointer", {}, db, "/tmp",
    )
    assert "error" in err


def test_delete_pointer_tool_registered():
    """98c71a42 — delete_sprint_item_pointer is a registered, non-read-only,
    destructive-hinted MCP tool (so it is callable and correctly annotated)."""
    from meridian.mcp_tools import _MCP_TOOLS_LIST, _READ_ONLY_TOOLS, _DESTRUCTIVE_TOOLS
    names = {t["name"] for t in _MCP_TOOLS_LIST}
    assert "delete_sprint_item_pointer" in names
    assert "delete_sprint_item_pointer" not in _READ_ONLY_TOOLS
    assert "delete_sprint_item_pointer" in _DESTRUCTIVE_TOOLS


# ---------------------------------------------------------------------------
# MCP tool-list + schema membership
# ---------------------------------------------------------------------------

def test_pointer_tools_in_mcp_tools_list():
    from meridian.mcp_tools import _MCP_TOOLS_LIST
    names = {t["name"] for t in _MCP_TOOLS_LIST}
    assert {"add_sprint_item_pointer", "get_sprint_item_pointers",
            "resolve_sprint_item_pointers"} <= names


def test_pointer_tools_do_not_require_project_id():
    from meridian.mcp_tools import _MCP_TOOLS_LIST
    by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    for name in ("add_sprint_item_pointer", "get_sprint_item_pointers",
                 "resolve_sprint_item_pointers"):
        schema = by_name[name]["inputSchema"]
        assert "project_name" in schema["properties"], name
        assert "project_id" not in (schema.get("required") or []), name


# ---------------------------------------------------------------------------
# S4 — text_quote (web, 1d3f6e71) + finding_id (experiment, 1f1cd4d9)
# ---------------------------------------------------------------------------

def test_validate_text_quote_and_finding_id_variants():
    ptr = validate_pointer({
        "source_type": "web",
        "targets": [{"uri": "https://example.com/a", "selector": {
            "type": "text_quote", "exact": "the cited passage",
            "prefix": "before ", "suffix": " after",
            "archived_url": "https://web.archive.org/web/2/https://example.com/a"}}],
    })
    sel = ptr["targets"][0]["selector"]
    assert sel["type"] == "text_quote"
    assert sel["exact"] == "the cited passage"
    assert sel["prefix"] == "before " and sel["suffix"] == " after"
    assert sel["archived_url"].startswith("https://web.archive.org/")

    ptr2 = validate_pointer({
        "source_type": "experiment",
        "targets": [{"uri": "finding:xyz",
                     "selector": {"type": "finding_id", "id": "note-123"}}],
    })
    assert ptr2["targets"][0]["selector"] == {"type": "finding_id", "id": "note-123"}


@pytest.mark.parametrize("bad_sel", [
    {"type": "text_quote"},                              # missing exact
    {"type": "text_quote", "exact": "   "},              # blank exact
    {"type": "text_quote", "exact": "x", "prefix": 5},   # non-str prefix
    {"type": "finding_id"},                              # missing id
    {"type": "finding_id", "id": ""},                    # empty id
])
def test_validate_rejects_bad_web_experiment_selectors(bad_sel):
    with pytest.raises(PointerValidationError):
        validate_pointer({"source_type": "x",
                          "targets": [{"uri": "u", "selector": bad_sel}]})


@pytest.mark.asyncio
async def test_resolve_text_quote_present_drift_and_guarded():
    ptr = {"source_type": "web", "targets": [{"uri": "https://x/a", "selector": {
        "type": "text_quote", "exact": "the cited passage",
        "archived_url": "https://web.archive.org/web/2/https://x/a"}}]}

    async def present(_uri): return "... the cited passage lives here ..."
    hit = (await resolve_pointer(None, ptr, web_fetcher=present))["targets"][0]
    assert hit["resolved"] is True and hit["found"] is True and hit["drift"] is False
    assert hit["archived_url"].startswith("https://web.archive.org/")

    async def changed(_uri): return "totally different content now"
    drift = (await resolve_pointer(None, ptr, web_fetcher=changed))["targets"][0]
    assert drift["resolved"] is True and drift["found"] is False and drift["drift"] is True

    async def nothing(_uri): return None
    n = (await resolve_pointer(None, ptr, web_fetcher=nothing))["targets"][0]
    assert n["resolved"] is False

    async def boom(_uri): raise RuntimeError("network")
    g = (await resolve_pointer(None, ptr, web_fetcher=boom))["targets"][0]
    assert g["resolved"] is False  # guarded — never raises
    assert g["archived_url"].startswith("https://web.archive.org/")  # echoed even unresolved


@pytest.mark.asyncio
async def test_resolve_text_quote_prefix_suffix_disambiguation():
    ptr = {"source_type": "web", "targets": [{"uri": "https://x/a", "selector": {
        "type": "text_quote", "exact": "bank", "prefix": "river ", "suffix": " side"}}]}
    async def right_context(_uri): return "walking along the river bank side at dusk"
    async def wrong_context(_uri): return "i deposited cash at the bank downtown"
    ok = (await resolve_pointer(None, ptr, web_fetcher=right_context))["targets"][0]
    assert ok["found"] is True
    miss = (await resolve_pointer(None, ptr, web_fetcher=wrong_context))["targets"][0]
    assert miss["found"] is False and miss["drift"] is True


@pytest.mark.asyncio
async def test_resolve_finding_id_hit_miss_and_guarded():
    ptr = {"source_type": "experiment", "targets": [
        {"uri": "finding:note-1", "selector": {"type": "finding_id", "id": "note-1"}},
        {"uri": "finding:nope", "selector": {"type": "finding_id", "id": "nope"}},
    ]}

    async def finder(_id):
        return ({"id": "note-1", "title": "Finding: exp run",
                 "body": "input=X\noutput=Y\nparams={'lr':0.1}"} if _id == "note-1" else None)
    hit, miss = (await resolve_pointer(None, ptr, finding_resolver=finder))["targets"]
    assert hit["resolved"] is True and hit["artifact"]["title"].startswith("Finding:")
    assert miss["resolved"] is False

    async def boom(_id): raise RuntimeError("db down")
    g = (await resolve_pointer(None, ptr, finding_resolver=boom))["targets"][0]
    assert g["resolved"] is False  # guarded


@pytest.mark.asyncio
async def test_web_archive_save_page_now_and_fetcher():
    from meridian import web_archive

    class _Resp:
        def __init__(self, headers=None, text=None):
            self.headers = headers or {}
            self.text = text

    async def post_ok(_url):
        return _Resp(headers={"Content-Location": "/web/20260706010101/https://example.com/a"})
    res = await web_archive.save_page_now("https://example.com/a", http_post=post_ok)
    assert res["archived_url"] == "https://web.archive.org/web/20260706010101/https://example.com/a"
    assert res["archived_at"]

    async def post_no_header(_url): return _Resp(headers={})
    res2 = await web_archive.save_page_now("https://example.com/a", http_post=post_no_header)
    assert res2["archived_url"] == "https://web.archive.org/web/2/https://example.com/a"

    async def post_boom(_url): raise RuntimeError("net")
    assert "error" in await web_archive.save_page_now("https://x", http_post=post_boom)

    assert web_archive.wayback_latest_url("https://x/a") == "https://web.archive.org/web/2/https://x/a"

    async def get_ok(_uri): return _Resp(text="page body")
    assert await web_archive.default_web_fetcher("https://x", http_get=get_ok) == "page body"

    async def get_boom(_uri): raise RuntimeError("net")
    assert await web_archive.default_web_fetcher("https://x", http_get=get_boom) is None


@pytest.mark.asyncio
async def test_mcp_web_pointer_archives_at_citation_time(db, monkeypatch):
    """1d3f6e71 — creating a source_type='web' text_quote pointer archives the URL
    at citation time (Save-Page-Now, stubbed) and stores the snapshot on the target."""
    from meridian import server as srv
    from meridian import web_archive

    async def _fake_spn(url, **_kw):
        return {"archived_url": f"https://web.archive.org/web/20260706/{url}",
                "archived_at": "2026-07-06 00:00:00"}
    monkeypatch.setattr(web_archive, "save_page_now", _fake_spn)

    p = await db_module.create_project(db, "web-ptr")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "cite a web source")
    added = await srv._dispatch_mcp_tool(
        "add_sprint_item_pointer",
        {"project_id": p["id"], "sprint_item_id": item["id"], "source_type": "web",
         "targets": [{"uri": "https://example.com/paper",
                      "selector": {"type": "text_quote", "exact": "a key claim"}}]},
        db, "/tmp",
    )
    sel = added["targets"][0]["selector"]
    assert sel["archived_url"] == "https://web.archive.org/web/20260706/https://example.com/paper"
    assert sel["archived_at"] == "2026-07-06 00:00:00"


@pytest.mark.asyncio
async def test_mcp_web_pointer_archive_failure_falls_back(db, monkeypatch):
    """Archiving is best-effort: an SPN failure still creates the pointer, with the
    deterministic Wayback 'latest snapshot' URL as the archive reference."""
    from meridian import server as srv
    from meridian import web_archive

    async def _spn_fails(url, **_kw):
        return {"error": "archive request failed"}
    monkeypatch.setattr(web_archive, "save_page_now", _spn_fails)

    p = await db_module.create_project(db, "web-ptr-fallback")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "cite")
    added = await srv._dispatch_mcp_tool(
        "add_sprint_item_pointer",
        {"project_id": p["id"], "sprint_item_id": item["id"], "source_type": "web",
         "targets": [{"uri": "https://example.com/x",
                      "selector": {"type": "text_quote", "exact": "claim"}}]},
        db, "/tmp",
    )
    assert added["targets"][0]["selector"]["archived_url"] == \
        "https://web.archive.org/web/2/https://example.com/x"


@pytest.mark.asyncio
async def test_mcp_experiment_pointer_resolves_save_finding_artifact(db):
    """1f1cd4d9 — a save_finding artifact (a run log: input/output/params/timestamp)
    is addressable via a source_type='experiment' finding_id pointer and resolves
    end-to-end through the real MCP dispatch (no injected seam)."""
    from meridian import server as srv

    p = await db_module.create_project(db, "exp-ptr")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "log an experiment")
    finding = await db_module.save_finding(
        db, p["id"],
        "experiment run 7\ninput=img_042.png\noutput=mask_042.png\nparams={'thresh':0.6}",
        source_type="experiment",
    )
    note_id = finding["note"]["id"]

    await srv._dispatch_mcp_tool(
        "add_sprint_item_pointer",
        {"project_id": p["id"], "sprint_item_id": item["id"], "source_type": "experiment",
         "targets": [{"uri": f"finding:{note_id}",
                      "selector": {"type": "finding_id", "id": note_id}}],
         "label": "run 7 artifact"},
        db, "/tmp",
    )
    resolved = await srv._dispatch_mcp_tool(
        "resolve_sprint_item_pointers",
        {"project_id": p["id"], "sprint_item_id": item["id"]},
        db, "/tmp",
    )
    target = resolved["pointers"][0]["targets"][0]
    assert target["resolved"] is True
    assert target["selector_type"] == "finding_id"
    assert target["artifact"]["id"] == note_id
    assert "input=img_042.png" in target["artifact"]["body"]


# ---------------------------------------------------------------------------
# e9d72d17 — selectable (not Zotero-hardcoded) reference-manager backend
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_citation_backend_registry_select_and_fallback():
    # Zotero ships registered as the default.
    assert "zotero" in pointers_module.available_citation_backends()
    assert pointers_module.DEFAULT_CITATION_BACKEND == "zotero"

    async def _fake(ref):
        return {"zotero_key": "X", "backend": "mendeley"}

    pointers_module.register_citation_backend("mendeley", lambda: _fake)
    try:
        assert "mendeley" in pointers_module.available_citation_backends()
        # Case-insensitive selection.
        assert pointers_module.resolve_citation_backend("MENDELEY") is _fake
        # Unknown backend → default (zotero) fallback, never an error.
        assert callable(pointers_module.resolve_citation_backend("nope-not-real"))
    finally:
        pointers_module._CITATION_BACKENDS.pop("mendeley", None)


def test_register_citation_backend_rejects_empty_name():
    with pytest.raises(ValueError):
        pointers_module.register_citation_backend("  ", lambda: None)


def test_resolve_citation_backend_env_var(monkeypatch):
    async def _fake(ref):
        return None

    pointers_module.register_citation_backend("acme", lambda: _fake)
    try:
        monkeypatch.setenv("MERIDIAN_CITATION_BACKEND", "acme")
        # No explicit arg → env var selects the backend.
        assert pointers_module.resolve_citation_backend(None) is _fake
        # Explicit arg beats the env var.
        assert pointers_module.resolve_citation_backend("zotero") is not _fake
    finally:
        pointers_module._CITATION_BACKENDS.pop("acme", None)


@pytest.mark.asyncio
async def test_resolve_pointer_routes_through_selected_backend():
    """resolve_pointer sends zotero_key targets through the SELECTED backend when no
    explicit citation_resolver is injected — the product-level selection seam."""
    async def _mendeley(ref):
        return {"zotero_key": "M1", "title": "via mendeley"} if ref == "zotero:M1" else None

    pointers_module.register_citation_backend("mendeley", lambda: _mendeley)
    try:
        ptr = {
            "source_type": "citation",
            "targets": [{"uri": "z", "selector": {"type": "zotero_key", "key": "M1"}}],
        }
        out = await resolve_pointer(
            None, ptr, project_id="pid",
            symbol_resolver=_stub_symbol_resolver,
            citation_backend="mendeley",
        )
        t = out["targets"][0]
        assert t["resolved"] is True
        assert t["item"]["title"] == "via mendeley"
    finally:
        pointers_module._CITATION_BACKENDS.pop("mendeley", None)


# ---------------------------------------------------------------------------
# 06df6ab3 — text_quote extended to anchor docx paragraph text (one selector
# mechanism across code/docs/web, not a new selector type).
# ---------------------------------------------------------------------------

def _docx_bytes(paragraphs: list[str]) -> bytes:
    """A minimal real .docx ZIP with one <w:p> per paragraph string."""
    import io
    import zipfile

    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
        for text in paragraphs
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def test_looks_like_local_docx_and_docx_paragraph_text(tmp_path):
    from meridian import web_archive

    assert web_archive._looks_like_local_docx("thesis/chapter1.docx") is True
    assert web_archive._looks_like_local_docx("https://x/a.docx") is False
    assert web_archive._looks_like_local_docx("https://x/a") is False
    assert web_archive._looks_like_local_docx("") is False
    assert web_archive._looks_like_local_docx(None) is False

    path = tmp_path / "sample.docx"
    path.write_bytes(_docx_bytes(["Intro paragraph.", "The cited passage lives here."]))
    text = web_archive._docx_paragraph_text(str(path))
    assert text == "Intro paragraph.\nThe cited passage lives here."

    # A missing/unreadable file degrades to None, never raises.
    assert web_archive._docx_paragraph_text(str(tmp_path / "missing.docx")) is None


@pytest.mark.asyncio
async def test_default_web_fetcher_routes_local_docx_to_paragraph_text(tmp_path):
    from meridian import web_archive

    path = tmp_path / "chapter1.docx"
    path.write_bytes(_docx_bytes(["Para one.", "Para two with the key claim."]))

    # No http_get involved for a local .docx path — it never touches the network.
    async def boom_if_called(_uri):
        raise AssertionError("HTTP fetch must not be used for a local .docx uri")

    text = await web_archive.default_web_fetcher(str(path), http_get=boom_if_called)
    assert text == "Para one.\nPara two with the key claim."

    # A plain http(s) URL still goes through the HTTP branch as before.
    class _Resp:
        def __init__(self, text):
            self.text = text

    async def get_ok(_uri):
        return _Resp("page body")

    assert await web_archive.default_web_fetcher("https://x/a.docx", http_get=get_ok) == "page body"


@pytest.mark.asyncio
async def test_resolve_text_quote_anchors_docx_paragraph_and_flags_drift(tmp_path):
    """End-to-end: a source_type='docs' text_quote pointer whose uri is a local
    .docx resolves via the SAME resolver as web text_quote — just fed docx
    paragraph text instead of a fetched page body — including drift detection
    when the passage no longer matches."""
    path = tmp_path / "chapter1.docx"
    path.write_bytes(_docx_bytes(["Intro.", "As shown by the key result in this section."]))

    ptr = {"source_type": "docs", "targets": [{"uri": str(path), "selector": {
        "type": "text_quote", "exact": "the key result"}}]}

    # No web_fetcher injected — resolve_pointer's default seam (web_archive's
    # default_web_fetcher) must route the local .docx path itself.
    out = await resolve_pointer(None, ptr)
    hit = out["targets"][0]
    assert hit["resolved"] is True
    assert hit["found"] is True
    assert hit["drift"] is False

    # A quote that no longer appears in the docx is a resolved drift, not an error.
    ptr2 = {"source_type": "docs", "targets": [{"uri": str(path), "selector": {
        "type": "text_quote", "exact": "a passage that was removed"}}]}
    out2 = await resolve_pointer(None, ptr2)
    miss = out2["targets"][0]
    assert miss["resolved"] is True
    assert miss["found"] is False
    assert miss["drift"] is True


# ---------------------------------------------------------------------------
# 3196ba0e — fail-closed artifact readiness verification (b730 follow-up)
#
# verify_target_readiness / verify_pointer_readiness answer the COMPLETION-
# time question "is this target genuinely ready?" — distinct from
# validate_pointer's opt-in, WRITE-time target_kind='existing' check above.
# meridian-outputs (figure_resolver / provenance_getter) is a separate
# package not importable from core, so these tests stub those seams exactly
# like _stub_symbol_resolver / _stub_node_resolver / _stub_citation_resolver
# do for resolve_pointer's own seams.
# ---------------------------------------------------------------------------

from meridian.pointers import verify_target_readiness, verify_pointer_readiness


@pytest.mark.asyncio
async def test_readiness_missing_uri_reported_explicitly():
    out = await verify_target_readiness({"target_kind": "existing"})
    assert out["ready"] is False
    assert out["status"] == "missing_uri"


@pytest.mark.asyncio
async def test_readiness_non_local_uri_skipped_not_faked_ready():
    """A zotero:/doc:/finding:/URL uri is out of scope for a filesystem
    readiness check — reported ready (skipped), never silently checked."""
    out = await verify_target_readiness({"uri": "zotero:ABCD1234", "target_kind": "existing"})
    assert out["ready"] is True
    assert out["status"] == "skipped"


@pytest.mark.asyncio
async def test_readiness_target_kind_omitted_defaults_to_existing(tmp_path):
    missing = tmp_path / "nope.py"
    out = await verify_target_readiness({"uri": str(missing)})
    assert out["target_kind"] == "existing"
    assert out["ready"] is False
    assert out["status"] == "missing"


@pytest.mark.asyncio
async def test_readiness_injectable_path_and_dir_checkers():
    """path_exists / is_dir are injectable seams, same pattern validate_pointer
    already uses for path_exists — tests never need to touch a real filesystem."""
    calls = []

    def _exists(uri):
        calls.append(("exists", uri))
        return True

    def _isdir(uri):
        calls.append(("isdir", uri))
        return False

    out = await verify_target_readiness(
        {"uri": "fake/path.csv", "target_kind": "existing"},
        path_exists=_exists, is_dir=_isdir,
    )
    assert out["ready"] is True
    assert ("exists", "fake/path.csv") in calls
    assert ("isdir", "fake/path.csv") in calls


# -- ba539706: normalized-spelling readiness (Windows/WSL/file:// parity) ----


@pytest.mark.asyncio
async def test_readiness_file_uri_resolves_against_bare_path_checker():
    """A file:// target resolves when the injected checker only recognizes
    the bare-path spelling (a plain os.path.exists-backed checker never
    understands file:// URIs directly) — reported uri stays the ORIGINAL
    file:// string, only the internal existence lookup is normalized."""
    bare = "C:/Users/alice/doc.docx"
    out = await verify_target_readiness(
        {"uri": "file:///C:/Users/alice/doc.docx", "target_kind": "existing"},
        path_exists=lambda uri: uri == bare,
        is_dir=lambda _uri: False,
    )
    assert out["ready"] is True
    assert out["status"] == "unresolved"  # no figure_resolver wired
    assert out["uri"] == "file:///C:/Users/alice/doc.docx"


@pytest.mark.asyncio
async def test_readiness_wsl_spelling_resolves_against_windows_style_checker():
    """A pointer recorded in the WSL /mnt/<drive> spelling is verified ready
    when the checker only recognizes the Windows-drive spelling — this is
    the exact 'meridian-docs opens it but readiness reports missing' bug
    ba539706 fixes."""
    windows_style = "C:\\Users\\alice\\doc.docx"
    out = await verify_target_readiness(
        {"uri": "/mnt/c/Users/alice/doc.docx", "target_kind": "existing"},
        path_exists=lambda uri: uri == windows_style,
        is_dir=lambda _uri: False,
    )
    assert out["ready"] is True
    assert out["status"] == "unresolved"


@pytest.mark.asyncio
async def test_readiness_is_dir_check_uses_matched_normalized_candidate():
    """The is_dir follow-up check runs against the SAME normalized candidate
    that matched existence, not the original (possibly-unrecognized) uri
    spelling."""
    windows_style = "C:\\Users\\alice\\outdir"
    isdir_calls = []

    def _isdir(uri):
        isdir_calls.append(uri)
        return True

    out = await verify_target_readiness(
        {"uri": "/mnt/c/Users/alice/outdir", "target_kind": "existing"},
        path_exists=lambda uri: uri == windows_style,
        is_dir=_isdir,
    )
    assert out["ready"] is False
    assert out["status"] == "is_directory"
    assert isdir_calls == [windows_style]


@pytest.mark.asyncio
async def test_readiness_normalization_never_widens_remote_uri_scope():
    """Fail-closed for genuinely unavailable remote targets is unchanged —
    a zotero:/doc:/finding:/URL uri is still reported 'skipped', never
    silently treated as a local candidate."""
    out = await verify_target_readiness(
        {"uri": "zotero:ABCD1234", "target_kind": "existing"},
        path_exists=lambda _uri: True,  # would wrongly pass if scope leaked
    )
    assert out["ready"] is True
    assert out["status"] == "skipped"


@pytest.mark.asyncio
async def test_readiness_missing_when_no_normalized_candidate_matches():
    """A target genuinely absent under every normalized spelling still
    reports 'missing' — normalization only widens accepted SPELLING, never
    what counts as present."""
    out = await verify_target_readiness(
        {"uri": "file:///C:/Users/alice/ghost.docx", "target_kind": "existing"},
        path_exists=lambda _uri: False,
    )
    assert out["ready"] is False
    assert out["status"] == "missing"
    assert out["uri"] == "file:///C:/Users/alice/ghost.docx"


# -- existing: file present / missing / is-a-directory -----------------------


@pytest.mark.asyncio
async def test_readiness_existing_file_present_no_resolver(tmp_path):
    """existing + file present + no figure_resolver -> ready, but explicitly
    'unresolved' (meridian-outputs unavailable) — never faked as canonical."""
    real = tmp_path / "results.csv"
    real.write_text("a,b\n1,2\n")
    out = await verify_target_readiness({"uri": str(real), "target_kind": "existing"})
    assert out["ready"] is True
    assert out["status"] == "unresolved"


@pytest.mark.asyncio
async def test_readiness_existing_missing_file(tmp_path):
    missing = tmp_path / "nope.csv"
    out = await verify_target_readiness({"uri": str(missing), "target_kind": "existing"})
    assert out["ready"] is False
    assert out["status"] == "missing"


@pytest.mark.asyncio
async def test_readiness_existing_path_is_a_directory(tmp_path):
    out = await verify_target_readiness({"uri": str(tmp_path), "target_kind": "existing"})
    assert out["ready"] is False
    assert out["status"] == "is_directory"


@pytest.mark.asyncio
async def test_readiness_planned_new_path_is_a_directory_before_provenance(tmp_path):
    """A planned_new target naming an existing DIRECTORY is rejected before
    provenance is even consulted."""
    async def _prov(_outputs_dir, _path):
        raise AssertionError("provenance_getter must not be called for a directory")

    out = await verify_target_readiness(
        {"uri": str(tmp_path), "target_kind": "planned_new"}, provenance_getter=_prov,
    )
    assert out["ready"] is False
    assert out["status"] == "is_directory"


# -- planned_new: creation + provenance registration --------------------------


@pytest.mark.asyncio
async def test_readiness_planned_new_not_created_yet(tmp_path):
    """Naming a future path is never enough on its own (the sprint spec's
    core requirement for this item)."""
    future = tmp_path / "not_written_yet.png"
    out = await verify_target_readiness({"uri": str(future), "target_kind": "planned_new"})
    assert out["ready"] is False
    assert out["status"] == "not_created"


@pytest.mark.asyncio
async def test_readiness_planned_new_before_record_provenance(tmp_path):
    """File was created, but record_provenance was never called for it — an
    in-memory ledger stub mirrors extensions/meridian-outputs' annotate.py
    record_provenance/get_provenance contract (path -> record dict | None)."""
    made = tmp_path / "figure_1.png"
    made.write_bytes(b"\x89PNG\r\n")
    ledger: dict = {}

    async def _get_provenance(_outputs_dir, path):
        return ledger.get(path)

    out = await verify_target_readiness(
        {"uri": str(made), "target_kind": "planned_new"}, provenance_getter=_get_provenance,
    )
    assert out["ready"] is False
    assert out["status"] == "provenance_missing"


@pytest.mark.asyncio
async def test_readiness_planned_new_after_record_provenance(tmp_path):
    """Once a provenance record exists for the same path, the SAME target
    flips to ready — mirroring record_provenance's real upsert-then-
    get_provenance round trip."""
    made = tmp_path / "figure_1.png"
    made.write_bytes(b"\x89PNG\r\n")
    ledger: dict = {}

    async def _get_provenance(_outputs_dir, path):
        return ledger.get(path)

    before = await verify_target_readiness(
        {"uri": str(made), "target_kind": "planned_new"}, provenance_getter=_get_provenance,
    )
    assert before["ready"] is False

    # Simulate record_provenance(outputs_dir, made, ...) having been called.
    ledger[str(made)] = {
        "path": str(made), "generating_script": "plot_results.py", "recorded_at": 1234.0,
    }

    after = await verify_target_readiness(
        {"uri": str(made), "target_kind": "planned_new"}, provenance_getter=_get_provenance,
    )
    assert after["ready"] is True
    assert after["status"] == "ready"
    assert after["provenance"]["generating_script"] == "plot_results.py"


@pytest.mark.asyncio
async def test_readiness_planned_new_provenance_getter_unavailable(tmp_path):
    """No provenance_getter wired at all (meridian-outputs unavailable) must
    degrade explicitly to ready=False — never silently pass."""
    made = tmp_path / "table_2.csv"
    made.write_text("x,y\n1,2\n")
    out = await verify_target_readiness({"uri": str(made), "target_kind": "planned_new"})
    assert out["ready"] is False
    assert out["status"] == "provenance_unavailable"


@pytest.mark.asyncio
async def test_readiness_planned_new_provenance_getter_raises_degrades(tmp_path):
    """A provenance_getter that raises (tool present but unreachable) must
    never be silently converted into success."""
    made = tmp_path / "table_3.csv"
    made.write_text("x,y\n1,2\n")

    async def _boom(_outputs_dir, _path):
        raise RuntimeError("meridian-outputs tunnel down")

    out = await verify_target_readiness(
        {"uri": str(made), "target_kind": "planned_new"}, provenance_getter=_boom,
    )
    assert out["ready"] is False
    assert out["status"] == "provenance_check_failed"
    assert "tunnel down" in out["reason"]


# -- existing: canonical vs archival vs ambiguous resolution ------------------


@pytest.mark.asyncio
async def test_readiness_existing_canonical_vs_archival_resolution(tmp_path):
    """canonical (non-archival) vs archival/stale classification is recorded,
    but BOTH stay ready=True — archival is deprioritized evidence, not a
    second gate (mirrors OutputsFtsIndex.search's own never-hard-exclude
    policy for archival rows)."""
    canon = tmp_path / "run.csv"
    canon.write_text("a,b\n1,2\n")
    stale = tmp_path / "run_old.csv"
    stale.write_text("a,b\n1,2\n")

    async def _resolver_canonical(_outputs_dir, file_path):
        return {"path": file_path, "is_archival": False, "canonical_path": None}

    async def _resolver_archival(_outputs_dir, file_path):
        return {"path": file_path, "is_archival": True, "canonical_path": str(canon)}

    canon_out = await verify_target_readiness(
        {"uri": str(canon), "target_kind": "existing"}, figure_resolver=_resolver_canonical,
    )
    assert canon_out["ready"] is True
    assert canon_out["status"] == "canonical"

    stale_out = await verify_target_readiness(
        {"uri": str(stale), "target_kind": "existing"}, figure_resolver=_resolver_archival,
    )
    assert stale_out["ready"] is True
    assert stale_out["status"] == "archival"
    assert stale_out["resolved"]["canonical_path"] == str(canon)


@pytest.mark.asyncio
async def test_readiness_existing_ambiguous_basename_resolution(tmp_path):
    """Multiple same-basename candidates (the meridian-outputs extension's
    relocation-tolerant basename-fallback tier) are surfaced as ambiguous,
    not silently collapsed to canonical."""
    figure = tmp_path / "plot.png"
    figure.write_bytes(b"\x89PNG\r\n")

    async def _resolver_ambiguous(_outputs_dir, file_path):
        return {"path": file_path, "is_archival": False,
                "match_type": "basename", "candidate_count": 3}

    out = await verify_target_readiness(
        {"uri": str(figure), "target_kind": "existing"}, figure_resolver=_resolver_ambiguous,
    )
    assert out["ready"] is True
    assert out["status"] == "ambiguous"
    assert out["resolved"]["candidate_count"] == 3


@pytest.mark.asyncio
async def test_readiness_existing_meridian_outputs_unavailable_no_resolver(tmp_path):
    """No figure_resolver at all — the tool genuinely unavailable. File
    presence still satisfies readiness, but status must say 'unresolved',
    never 'canonical' (never fake success for an unreachable check)."""
    real = tmp_path / "output.npy"
    real.write_bytes(b"\x93NUMPY")
    out = await verify_target_readiness({"uri": str(real), "target_kind": "existing"})
    assert out["ready"] is True
    assert out["status"] == "unresolved"
    assert "unavailable" in out["reason"]


@pytest.mark.asyncio
async def test_readiness_existing_meridian_outputs_resolver_raises_degrades(tmp_path):
    """A figure_resolver that raises (tool present but unreachable) degrades
    explicitly rather than silently reporting canonical."""
    real = tmp_path / "output.json"
    real.write_text("{}")

    async def _boom(_outputs_dir, _path):
        raise RuntimeError("outputs tunnel timeout")

    out = await verify_target_readiness(
        {"uri": str(real), "target_kind": "existing"}, figure_resolver=_boom,
    )
    assert out["ready"] is True
    assert out["status"] == "degraded"
    assert "timeout" in out["reason"]


@pytest.mark.asyncio
async def test_readiness_default_figure_resolver_wraps_outputs_indexer(tmp_path):
    """The core-local default figure_resolver (used when a caller wants real
    resolution without injecting a stub) really does reuse
    outputs_indexer.resolve_figure_output rather than duplicating resolution
    policy — proven end-to-end against a real (tiny) outputs tree."""
    outputs_dir = tmp_path / "outputs"
    outputs_dir.mkdir()
    csv_path = outputs_dir / "metrics.csv"
    csv_path.write_text("epoch,loss\n1,0.5\n")

    resolver = pointers_module._default_figure_resolver()
    out = await verify_target_readiness(
        {"uri": str(csv_path), "target_kind": "existing"},
        outputs_dir=str(outputs_dir), figure_resolver=resolver,
    )
    assert out["ready"] is True
    assert out["status"] == "canonical"
    assert out["resolved"]["path"] == str(csv_path)


# -- pointer-level wrapper -----------------------------------------------------


@pytest.mark.asyncio
async def test_readiness_pointer_level_requires_every_target_ready(tmp_path):
    good = tmp_path / "good.csv"
    good.write_text("a\n1\n")
    missing = tmp_path / "missing.csv"

    ptr = {
        "source_type": "experiment",
        "label": "run artifacts",
        "targets": [
            {"uri": str(good), "target_kind": "existing"},
            {"uri": str(missing), "target_kind": "existing"},
        ],
    }
    out = await verify_pointer_readiness(ptr)
    assert out["ready"] is False
    assert out["label"] == "run artifacts"
    assert out["targets"][0]["ready"] is True
    assert out["targets"][1]["ready"] is False
    assert out["targets"][1]["status"] == "missing"


@pytest.mark.asyncio
async def test_readiness_pointer_level_all_ready_when_every_target_passes(tmp_path):
    a = tmp_path / "a.csv"
    a.write_text("x\n")
    b = tmp_path / "b.png"
    b.write_bytes(b"\x89PNG")
    ptr = {"source_type": "experiment", "targets": [
        {"uri": str(a), "target_kind": "existing"},
        {"uri": str(b), "target_kind": "existing"},
    ]}
    out = await verify_pointer_readiness(ptr)
    assert out["ready"] is True
    assert all(t["ready"] for t in out["targets"])


@pytest.mark.asyncio
async def test_readiness_pointer_level_empty_targets_never_vacuously_ready():
    out = await verify_pointer_readiness({"source_type": "experiment", "targets": []})
    assert out["ready"] is False
    assert out["targets"] == []


@pytest.mark.asyncio
async def test_readiness_pointer_level_malformed_target_never_raises():
    out = await verify_pointer_readiness({"source_type": "x", "targets": ["not-a-dict"]})
    assert out["ready"] is False
    assert out["targets"][0]["status"] == "malformed_target"


# ---------------------------------------------------------------------------
# 88f82c15 (b730 follow-up) — evaluate_artifact_pointer_policy: the warn/
# strict POLICY evaluator that runs at handoff-ANNOTATION time, distinct
# from (and built on top of) verify_target_readiness/verify_pointer_readiness
# above (a completion-time, per-target, I/O-backed check) and the 5fd9d2fd
# classifier (meridian.artifact_classification.classify_artifact_work, which
# this evaluator reuses rather than duplicating).
#
# Covers: off/warn/strict mode behavior, every insufficiency reason code
# (bare docx / directory / generic tool reference / unsupported type /
# missing entirely), the "cannot self-declare out of the check" invariant,
# and the false-positive exception (a genuinely document_only/caption_only
# item with a bare/insufficient pointer must NEVER warn).
# ---------------------------------------------------------------------------

import json as _json

from meridian.pointers import evaluate_artifact_pointer_policy


_STRICT_POLICY = _json.dumps({"artifact_pointer_check": "strict"})
_WARN_POLICY = _json.dumps({"artifact_pointer_check": "warn"})
_OFF_POLICY = _json.dumps({"artifact_pointer_check": "off"})


def _figure_item(**overrides):
    item = {"id": "art-1", "title": "Insert a new ablation chart figure into the results"}
    item.update(overrides)
    return item


# --- required result shape -------------------------------------------------

def test_evaluate_artifact_pointer_policy_always_returns_required_fields():
    """Each result must include: item id, classification, policy, warning
    code, required remediation, and affected pointer ids."""
    result = evaluate_artifact_pointer_policy(_figure_item())
    for key in (
        "item_id", "classification", "policy",
        "warning_code", "required_remediation", "affected_pointer_ids",
    ):
        assert key in result
    assert result["item_id"] == "art-1"
    assert isinstance(result["classification"], dict)
    assert isinstance(result["policy"], dict)


def test_evaluate_artifact_pointer_policy_never_raises_on_malformed_item():
    result = evaluate_artifact_pointer_policy(None)  # type: ignore[arg-type]
    assert result["warning_code"] is None
    assert result["ready"] is True
    result2 = evaluate_artifact_pointer_policy({})
    assert result2["warning_code"] is None


# --- default policy is warn -------------------------------------------------

def test_default_artifact_pointer_check_is_warn_when_undeclared():
    result = evaluate_artifact_pointer_policy(_figure_item())
    assert result["policy"]["artifact_pointer_check"] == "warn"
    assert result["warning_code"] == "missing_pointer"
    assert result["ready"] is True  # warn mode never blocks


# --- not artifact-sensitive: never warns, regardless of policy -------------

def test_not_sensitive_item_never_warns_even_under_strict():
    item = _figure_item(
        title="Renumber figure captions after Figure 4 was deleted",
        artifact_policy=_STRICT_POLICY,
    )
    result = evaluate_artifact_pointer_policy(item)
    assert result["classification"]["classification"] == "caption_only"
    assert result["classification"]["is_artifact_sensitive"] is False
    assert result["warning_code"] is None
    assert result["ready"] is True


def test_false_positive_document_only_declared_kind_with_bare_pointer_never_warns():
    """A genuinely document_only item (declared kind wins, per 5fd9d2fd) with
    a bare .docx pointer must NOT warn, even under strict policy."""
    item = _figure_item(
        title="Insert a new ablation chart figure",  # figure-sounding title
        artifact_kind="document_only",  # explicit override — genuinely document_only
        touches_resources=_json.dumps(["file:outputs/report.docx"]),
        artifact_policy=_STRICT_POLICY,
    )
    result = evaluate_artifact_pointer_policy(item)
    assert result["classification"]["classification"] == "document_only"
    assert result["classification"]["rule"] == "declared_artifact_kind"
    assert result["warning_code"] is None
    assert result["ready"] is True


def test_false_positive_fallback_caption_only_with_bare_pointer_never_warns():
    item = _figure_item(
        title="Renumber figure captions",
        touches_resources=_json.dumps(["file:outputs/report.docx"]),
        artifact_policy=_STRICT_POLICY,
    )
    result = evaluate_artifact_pointer_policy(item)
    assert result["classification"]["classification"] == "caption_only"
    assert result["warning_code"] is None


# --- a figure/table item cannot self-declare its way out --------------------

def test_allow_document_only_override_does_not_bypass_a_sensitive_verdict():
    """policy.allow_document_only_override is NOT consulted to flip a
    genuinely sensitive (figure/table) classification to safe — only the
    classifier's own verdict (declared kind, or fallback evidence) can do
    that. A figure/table item cannot self-declare its way out of the check."""
    item = _figure_item(
        artifact_policy=_json.dumps({
            "artifact_pointer_check": "strict",
            "allow_document_only_override": True,
        }),
    )
    result = evaluate_artifact_pointer_policy(item)
    assert result["classification"]["is_artifact_sensitive"] is True
    assert result["warning_code"] == "missing_pointer"
    assert result["ready"] is False


# --- insufficiency reason codes ---------------------------------------------

def test_insufficient_bare_docx_pointer_warns_with_specific_code():
    item = _figure_item(
        touches_resources=_json.dumps(["file:outputs/report.docx"]),
        artifact_policy=_WARN_POLICY,
    )
    result = evaluate_artifact_pointer_policy(item)
    assert result["warning_code"] == "insufficient_pointer_bare_docx"
    assert result["required_remediation"]
    assert "docx" in result["required_remediation"].lower()
    assert result["ready"] is True  # warn mode


def test_insufficient_directory_pointer_warns_with_specific_code():
    item = _figure_item(
        touches_resources=_json.dumps(["file:outputs/figures/"]),
        artifact_policy=_WARN_POLICY,
    )
    result = evaluate_artifact_pointer_policy(item)
    assert result["warning_code"] == "insufficient_pointer_directory"


def test_insufficient_generic_tool_reference_warns_and_names_pointer_id():
    item = _figure_item(
        pointer_records=[{
            "id": "ptr-abc123",
            "source_type": "code",
            "targets": [{"uri": "mcp_tool:search_outputs"}],
        }],
        artifact_policy=_WARN_POLICY,
    )
    result = evaluate_artifact_pointer_policy(item)
    assert result["warning_code"] == "insufficient_pointer_generic_reference"
    assert result["affected_pointer_ids"] == ["ptr-abc123"]


def test_insufficient_unsupported_extension_warns_with_specific_code():
    item = _figure_item(
        touches_resources=_json.dumps(["file:outputs/figures/notes.txt"]),
        artifact_policy=_WARN_POLICY,
    )
    result = evaluate_artifact_pointer_policy(item)
    assert result["warning_code"] == "insufficient_pointer_unsupported_type"


def test_missing_pointer_entirely_uses_missing_pointer_code():
    item = _figure_item(artifact_policy=_WARN_POLICY)
    result = evaluate_artifact_pointer_policy(item)
    assert result["warning_code"] == "missing_pointer"
    assert result["affected_pointer_ids"] == []


def test_concrete_evidence_never_warns_even_under_strict():
    item = _figure_item(
        touches_resources=_json.dumps(["file:outputs/figures/ablation.png"]),
        artifact_policy=_STRICT_POLICY,
    )
    result = evaluate_artifact_pointer_policy(item)
    assert result["warning_code"] is None
    assert result["ready"] is True


# --- off/warn/strict mode matrix --------------------------------------------

def test_warn_mode_emits_warning_but_stays_ready():
    item = _figure_item(artifact_policy=_WARN_POLICY)
    result = evaluate_artifact_pointer_policy(item)
    assert result["warning_code"] is not None
    assert result["ready"] is True


def test_strict_mode_emits_warning_and_is_not_ready():
    item = _figure_item(artifact_policy=_STRICT_POLICY)
    result = evaluate_artifact_pointer_policy(item)
    assert result["warning_code"] is not None
    assert result["ready"] is False


def test_off_mode_suppresses_warning_but_preserves_classification_and_policy():
    """off mode: the policy warning is suppressed while raw declarations
    (classification + effective policy) are still preserved."""
    item = _figure_item(artifact_policy=_OFF_POLICY)
    result = evaluate_artifact_pointer_policy(item)
    assert result["warning_code"] is None
    assert result["required_remediation"] is None
    assert result["affected_pointer_ids"] == []
    assert result["ready"] is True
    # "raw declarations... preserved" — the real classification/policy are
    # NOT replaced with empty/unknown placeholders just because checking is off.
    assert result["classification"]["classification"] == "figure"
    assert result["classification"]["is_artifact_sensitive"] is True
    assert result["policy"]["artifact_pointer_check"] == "off"


def test_off_mode_with_insufficient_pointer_also_suppresses():
    item = _figure_item(
        touches_resources=_json.dumps(["file:outputs/report.docx"]),
        artifact_policy=_OFF_POLICY,
    )
    result = evaluate_artifact_pointer_policy(item)
    assert result["warning_code"] is None
    assert result["ready"] is True


# ---------------------------------------------------------------------------
# 70c10ca3 (b730 follow-up) — build_artifact_pointer_finding /
# assemble_artifact_pointer_findings_from_annotated_items: combine 88f82c15's
# warn/strict policy verdict with 3196ba0e's fail-closed readiness
# verification into ONE canonical, machine-readable finding.
# ---------------------------------------------------------------------------

from meridian.pointers import build_artifact_pointer_finding  # noqa: E402


@pytest.mark.asyncio
async def test_build_artifact_pointer_finding_none_when_no_active_warning():
    """A genuinely non-sensitive item produces no finding at all — mirrors
    evaluate_artifact_pointer_policy's own restraint."""
    item = {"id": "art-1", "title": "Renumber figure captions"}
    out = await build_artifact_pointer_finding(item)
    assert out is None


@pytest.mark.asyncio
async def test_build_artifact_pointer_finding_missing_pointer_status():
    """No candidate pointer at all -> pointer_status 'missing', and an empty
    target_readiness (there is no durable row to verify)."""
    item = _figure_item(artifact_policy=_WARN_POLICY)
    out = await build_artifact_pointer_finding(item)
    assert out is not None
    assert out["warning_code"] == "missing_pointer"
    assert out["pointer_status"] == "missing"
    assert out["affected_pointer_ids"] == []
    assert out["target_readiness"] == []


@pytest.mark.asyncio
async def test_build_artifact_pointer_finding_weak_pointer_status_and_readiness(tmp_path):
    """A bare .docx durable pointer -> pointer_status 'weak', and its
    readiness IS verified (via the default core-local figure_resolver) for
    the implicated pointer id."""
    docx = tmp_path / "report.docx"
    docx.write_bytes(b"PK\x03\x04")  # a real file on disk
    item = _figure_item(
        artifact_policy=_STRICT_POLICY,
        pointer_records=[{
            "id": "ptr-1",
            "source_type": "docs",
            "targets": [{"uri": str(docx), "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
        }],
    )
    stored_pointers = [{"id": "ptr-1", "targets": [{"uri": str(docx), "target_kind": "existing"}]}]
    out = await build_artifact_pointer_finding(item, stored_pointers=stored_pointers)
    assert out is not None
    assert out["warning_code"] == "insufficient_pointer_bare_docx"
    assert out["pointer_status"] == "weak"
    assert out["affected_pointer_ids"] == ["ptr-1"]
    assert len(out["target_readiness"]) == 1
    entry = out["target_readiness"][0]
    assert entry["pointer_id"] == "ptr-1"
    # entry is the FULL verify_pointer_readiness result for this pointer
    # (pointer-level ready + a per-target verdict list), plus pointer_id.
    assert entry["ready"] is True
    assert len(entry["targets"]) == 1
    target_verdict = entry["targets"][0]
    # The DEFAULT figure_resolver was genuinely consulted (not merely
    # "figure_resolver is None -> unavailable") — proven by the distinct
    # "not found in the meridian-outputs index" reason text only that branch
    # produces (see verify_target_readiness).
    assert target_verdict["status"] == "unresolved"
    assert "meridian-outputs index" in target_verdict["reason"]


@pytest.mark.asyncio
async def test_build_artifact_pointer_finding_target_readiness_missing_when_file_absent(tmp_path):
    absent = tmp_path / "does_not_exist.docx"
    item = _figure_item(
        artifact_policy=_STRICT_POLICY,
        pointer_records=[{
            "id": "ptr-missing",
            "source_type": "docs",
            "targets": [{"uri": str(absent), "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
        }],
    )
    stored_pointers = [{"id": "ptr-missing", "targets": [{"uri": str(absent), "target_kind": "existing"}]}]
    out = await build_artifact_pointer_finding(item, stored_pointers=stored_pointers)
    entry = out["target_readiness"][0]
    assert entry["ready"] is False
    assert entry["targets"][0]["status"] == "missing"


@pytest.mark.asyncio
async def test_build_artifact_pointer_finding_consumes_injected_figure_resolver(tmp_path):
    """canonical / archival / ambiguous all propagate through target_readiness
    when a caller injects a stub figure_resolver — proving readiness data,
    not just file-presence, is genuinely consumed end to end."""
    stale = tmp_path / "run_old.docx"
    stale.write_bytes(b"PK\x03\x04")
    item = _figure_item(
        artifact_policy=_STRICT_POLICY,
        pointer_records=[{
            "id": "ptr-archival",
            "source_type": "docs",
            "targets": [{"uri": str(stale), "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
        }],
    )
    stored_pointers = [{"id": "ptr-archival", "targets": [{"uri": str(stale), "target_kind": "existing"}]}]

    async def _archival_resolver(_outputs_dir, file_path):
        return {"path": file_path, "is_archival": True, "canonical_path": "run.docx"}

    out = await build_artifact_pointer_finding(
        item, stored_pointers=stored_pointers, figure_resolver=_archival_resolver,
    )
    entry = out["target_readiness"][0]
    assert entry["ready"] is True
    target_verdict = entry["targets"][0]
    assert target_verdict["status"] == "archival"
    assert target_verdict["resolved"]["canonical_path"] == "run.docx"


@pytest.mark.asyncio
async def test_build_artifact_pointer_finding_reuses_supplied_policy_result_verbatim():
    """policy_result, when given, is used AS-IS — no independent
    recomputation from `item` (proven by an item that would otherwise
    classify as non-sensitive/None)."""
    canned_policy = {
        "item_id": "hand-crafted",
        "classification": {"classification": "table", "is_artifact_sensitive": True},
        "policy": {"artifact_pointer_check": "warn"},
        "warning_code": "insufficient_pointer_directory",
        "required_remediation": "point at the file, not the directory",
        "affected_pointer_ids": [],
        "ready": True,
    }
    out = await build_artifact_pointer_finding({}, policy_result=canned_policy)
    assert out["item_id"] == "hand-crafted"
    assert out["warning_code"] == "insufficient_pointer_directory"
    assert out["pointer_status"] == "weak"
    assert out["target_readiness"] == []


@pytest.mark.asyncio
async def test_build_artifact_pointer_finding_multiple_affected_ids_sorted(tmp_path):
    a = tmp_path / "a.docx"
    a.write_bytes(b"PK")
    b = tmp_path / "b.docx"
    b.write_bytes(b"PK")
    canned_policy = {
        "item_id": "multi",
        "classification": {}, "policy": {},
        "warning_code": "insufficient_pointer_bare_docx",
        "required_remediation": "x",
        "affected_pointer_ids": ["ptr-b", "ptr-a"],
        "ready": True,
    }
    stored_pointers = [
        {"id": "ptr-b", "targets": [{"uri": str(b), "target_kind": "existing"}]},
        {"id": "ptr-a", "targets": [{"uri": str(a), "target_kind": "existing"}]},
    ]
    out = await build_artifact_pointer_finding(
        {}, policy_result=canned_policy, stored_pointers=stored_pointers,
    )
    assert [t["pointer_id"] for t in out["target_readiness"]] == ["ptr-a", "ptr-b"]


@pytest.mark.asyncio
async def test_build_artifact_pointer_finding_readiness_failure_degrades_not_raises(monkeypatch):
    canned_policy = {
        "item_id": "boom-item",
        "classification": {}, "policy": {},
        "warning_code": "insufficient_pointer_bare_docx",
        "required_remediation": "x",
        "affected_pointer_ids": ["ptr-boom"],
        "ready": True,
    }
    stored_pointers = [{"id": "ptr-boom", "targets": [{"uri": "x.docx", "target_kind": "existing"}]}]

    async def _boom(*_a, **_k):
        raise RuntimeError("readiness check exploded")

    monkeypatch.setattr(pointers_module, "verify_pointer_readiness", _boom)
    out = await build_artifact_pointer_finding(
        {}, policy_result=canned_policy, stored_pointers=stored_pointers,
    )
    assert out is not None
    entry = out["target_readiness"][0]
    assert entry["pointer_id"] == "ptr-boom"
    assert entry["ready"] is False
    assert entry["status"] == "verification_error"


@pytest.mark.asyncio
async def test_build_artifact_pointer_finding_missing_affected_pointer_id_skipped():
    """An affected_pointer_ids entry with no matching stored_pointers row is
    silently skipped (never crashes, never fabricates a verdict) — even when
    OTHER stored_pointers rows exist (proves it's an id-match miss, not just
    an empty stored_pointers short-circuit)."""
    canned_policy = {
        "item_id": "orphan",
        "classification": {}, "policy": {},
        "warning_code": "insufficient_pointer_directory",
        "required_remediation": "x",
        "affected_pointer_ids": ["ptr-does-not-exist"],
        "ready": True,
    }
    out = await build_artifact_pointer_finding(
        {}, policy_result=canned_policy,
        stored_pointers=[{"id": "ptr-unrelated", "targets": [{"uri": "x.docx", "target_kind": "existing"}]}],
    )
    assert out["target_readiness"] == []


def test_build_artifact_pointer_finding_never_raises_on_evaluate_blowup(monkeypatch):
    """A raising evaluate_artifact_pointer_policy (no policy_result supplied)
    degrades to None, never crashes this mandatory annotation path."""
    def _boom(_item):
        raise RuntimeError("classification exploded")

    monkeypatch.setattr(pointers_module, "evaluate_artifact_pointer_policy", _boom)
    import asyncio as _asyncio
    out = _asyncio.run(build_artifact_pointer_finding({"id": "x"}))
    assert out is None


# ---------------------------------------------------------------------------
# assemble_artifact_pointer_findings_from_annotated_items — pure assembly
# ---------------------------------------------------------------------------

from meridian.pointers import (  # noqa: E402
    assemble_artifact_pointer_findings_from_annotated_items,
)


def test_assemble_artifact_pointer_findings_empty_when_none_active():
    items = [{"id": "a", "title": "no problem here"}]
    assert assemble_artifact_pointer_findings_from_annotated_items(items) == []


def test_assemble_artifact_pointer_findings_skips_items_with_none_finding():
    items = [{"id": "a", "artifact_pointer_finding": None}]
    assert assemble_artifact_pointer_findings_from_annotated_items(items) == []


def test_assemble_artifact_pointer_findings_sorted_by_item_id():
    items = [
        {"id": "zzz", "artifact_pointer_finding": {"item_id": "zzz", "warning_code": "missing_pointer"}},
        {"id": "aaa", "artifact_pointer_finding": {"item_id": "aaa", "warning_code": "missing_pointer"}},
    ]
    out = assemble_artifact_pointer_findings_from_annotated_items(items)
    assert [e["item_id"] for e in out] == ["aaa", "zzz"]


def test_assemble_artifact_pointer_findings_ignores_malformed_entries():
    items = [
        "not-a-dict",
        {"artifact_pointer_finding": {"item_id": "no-id"}},  # missing top-level id
        {"id": "ok", "artifact_pointer_finding": {"item_id": "ok", "warning_code": "missing_pointer"}},
    ]
    out = assemble_artifact_pointer_findings_from_annotated_items(items)
    assert [e["item_id"] for e in out] == ["ok"]


# ---------------------------------------------------------------------------
# eb8b6894 — distinguish pointer PRESENCE from successful TARGET RESOLUTION.
#
# Confirmed bug: a checkpoint/handoff projection could show a durable pointer
# row EXISTS and mark provenance "satisfied" even when resolve_pointer
# reports every target unresolved. These tests exercise the three new
# explicit signals (structural_valid / target_resolved / provenance_verified),
# the live-vs-fallback resolution_source distinction, the item-level rollup
# (pointers.aggregate_pointer_evidence), and the opt-in STRICT gate
# (db.is_item_claim_prospected(strict=True, target_resolved=...)) that makes
# an unresolved-but-present pointer fail a strict caller instead of quietly
# passing.
# ---------------------------------------------------------------------------

from meridian.pointers import (  # noqa: E402
    check_structural_validity,
    build_typed_pointer_record,
    aggregate_pointer_evidence,
    verify_pointer_readiness,
    default_figure_resolver,
    compute_pointer_readiness_for_record,
)
from meridian import handoff as handoff_module  # noqa: E402


def test_check_structural_validity_pure_pass_and_fail():
    ok = {"source_type": "code", "targets": [
        {"uri": "a.py", "selector": {"type": "range", "start_line": 1, "end_line": 2},
         "target_kind": "existing"},
    ]}
    valid, err = check_structural_validity(ok)
    assert valid is True and err is None

    bad = {"source_type": "code", "targets": [
        {"uri": "a.py", "selector": {"type": "bogus"}},
    ]}
    valid, err = check_structural_validity(bad)
    assert valid is False
    assert "bogus" in err


def test_check_structural_validity_never_touches_real_filesystem():
    """A STORED pointer (row_to_pointer shape) always carries an EXPLICIT
    target_kind on every target — re-validating it with the REAL filesystem
    checker would retroactively disk-check every implicit-'existing' pointer
    ever written (see the module docstring). check_structural_validity must
    stay pure: a target_kind='existing' pointer at a path that does NOT
    exist on disk must still be reported structurally valid."""
    stored = {
        "source_type": "code",
        "targets": [{
            "uri": "definitely/does/not/exist/on/this/machine.py",
            "selector": {"type": "range", "start_line": 1, "end_line": 2},
            "target_kind": "existing",
        }],
    }
    valid, err = check_structural_validity(stored)
    assert valid is True and err is None


def test_build_typed_pointer_record_target_resolved_true_for_fully_resolved_pointer():
    stored = {"source_type": "code", "targets": [
        {"uri": "a.py", "selector": {"type": "range", "start_line": 1, "end_line": 2},
         "target_kind": "existing"},
    ]}
    resolved = {"source_type": "code", "targets": [
        {"resolved": True, "selector_type": "range", "uri": "a.py",
         "range": {"start_line": 1, "end_line": 2}},
    ]}
    rec = build_typed_pointer_record(stored, resolved)
    assert rec["structural_valid"] is True
    assert "structural_error" not in rec
    assert rec["target_resolved"] is True
    assert "target_resolved_reason" not in rec
    # range has no live/fallback resolution concept.
    assert rec["resolution_source"] == "not_applicable"
    # No readiness was supplied — tri-state None, never coerced to False.
    assert rec["provenance_verified"] is None
    assert rec["provenance_reason"]


def test_build_typed_pointer_record_target_resolved_false_carries_reason():
    """The core bug scenario: a durable pointer row (structurally valid) whose
    target the resolver could NOT find — target_resolved must be False with
    an explicit reason, distinct from structural_valid (still True: the
    pointer itself is well-formed)."""
    stored = {"source_type": "code", "targets": [
        {"uri": "file:x.py", "selector": {"type": "symbol", "qualified_name": "x.missing"},
         "target_kind": "existing"},
    ]}
    resolved = {"source_type": "code", "targets": [
        {"resolved": False, "selector_type": "symbol", "uri": "file:x.py",
         "qualified_name": "x.missing", "reason": "no matching symbol in graph snapshot"},
    ]}
    rec = build_typed_pointer_record(stored, resolved)
    assert rec["structural_valid"] is True
    assert rec["target_resolved"] is False
    assert "unresolved" in rec["target_resolved_reason"]
    assert "no matching symbol in graph snapshot" in rec["target_resolved_reason"]


def test_build_typed_pointer_record_target_resolved_false_with_no_resolve_pass_at_all():
    """resolved=None (never resolved) must also read as NOT resolved — never
    vacuously True just because nothing failed."""
    stored = {"source_type": "code", "targets": [
        {"uri": "a.py", "selector": {"type": "range", "start_line": 1, "end_line": 2}},
    ]}
    rec = build_typed_pointer_record(stored, None)
    assert rec["target_resolved"] is False
    assert "not been resolved" in rec["target_resolved_reason"]


def test_build_typed_pointer_record_resolution_source_from_symbol_match():
    stored = {"source_type": "code", "targets": [
        {"uri": "a.py", "selector": {"type": "symbol", "qualified_name": "a.b"},
         "target_kind": "existing"},
    ]}
    resolved_live = {"source_type": "code", "targets": [
        {"resolved": True, "selector_type": "symbol", "uri": "a.py",
         "qualified_name": "a.b", "resolution_source": "live_graph"},
    ]}
    assert build_typed_pointer_record(stored, resolved_live)["resolution_source"] == "live_graph"

    resolved_stale = {"source_type": "code", "targets": [
        {"resolved": True, "selector_type": "symbol", "uri": "a.py",
         "qualified_name": "a.b", "resolution_source": "stale_snapshot"},
    ]}
    assert build_typed_pointer_record(stored, resolved_stale)["resolution_source"] == "stale_snapshot"


def test_build_typed_pointer_record_resolution_source_mixed_across_targets():
    stored = {"source_type": "code", "targets": [
        {"uri": "a.py", "selector": {"type": "symbol", "qualified_name": "a.b"}},
        {"uri": "b.py", "selector": {"type": "symbol", "qualified_name": "c.d"}},
    ]}
    resolved = {"source_type": "code", "targets": [
        {"resolved": True, "selector_type": "symbol", "uri": "a.py",
         "qualified_name": "a.b", "resolution_source": "live_graph"},
        {"resolved": True, "selector_type": "symbol", "uri": "b.py",
         "qualified_name": "c.d", "resolution_source": "stale_snapshot"},
    ]}
    rec = build_typed_pointer_record(stored, resolved)
    assert rec["resolution_source"] == "mixed"


def test_build_typed_pointer_record_provenance_verified_none_without_readiness():
    stored = {"source_type": "code", "targets": [
        {"uri": "a.py", "selector": {"type": "range", "start_line": 1, "end_line": 1},
         "target_kind": "existing"},
    ]}
    rec = build_typed_pointer_record(stored, None, readiness=None)
    assert rec["provenance_verified"] is None
    assert "not computed" in rec["provenance_reason"]


@pytest.mark.asyncio
async def test_build_typed_pointer_record_provenance_verified_reflects_readiness(tmp_path):
    """Wires an ACTUAL verify_pointer_readiness result in: a range selector
    always resolves (target_resolved True) regardless of whether the file
    exists — provenance_verified is the SEPARATE signal that catches that,
    proving all three fields answer genuinely different questions."""
    missing = str(tmp_path / "does_not_exist.py")
    stored = {"source_type": "code", "targets": [
        {"uri": missing, "selector": {"type": "range", "start_line": 1, "end_line": 1},
         "target_kind": "existing"},
    ]}
    resolved = {"source_type": "code", "targets": [
        {"resolved": True, "selector_type": "range", "uri": missing,
         "range": {"start_line": 1, "end_line": 1}},
    ]}
    readiness = await verify_pointer_readiness(stored)
    rec = build_typed_pointer_record(stored, resolved, readiness=readiness)
    # target_resolved is True (range resolves unconditionally)...
    assert rec["target_resolved"] is True
    # ...but provenance_verified correctly catches the missing file.
    assert rec["provenance_verified"] is False
    assert rec["provenance_reason"]

    real_file = tmp_path / "real.py"
    real_file.write_text("x = 1\n")
    stored_real = {"source_type": "code", "targets": [
        {"uri": str(real_file), "selector": {"type": "range", "start_line": 1, "end_line": 1},
         "target_kind": "existing"},
    ]}
    readiness_real = await verify_pointer_readiness(stored_real)
    rec_real = build_typed_pointer_record(stored_real, resolved, readiness=readiness_real)
    assert rec_real["provenance_verified"] is True
    assert "provenance_reason" not in rec_real


@pytest.mark.asyncio
async def test_compute_pointer_readiness_for_record_never_raises_on_malformed_pointer():
    # No 'targets' key at all — verify_pointer_readiness degrades gracefully.
    out = await compute_pointer_readiness_for_record({"source_type": "code"})
    assert isinstance(out, dict)
    assert out["ready"] is False


def test_default_figure_resolver_returns_callable():
    resolver = default_figure_resolver()
    assert callable(resolver)


# ---------------------------------------------------------------------------
# aggregate_pointer_evidence — item-level rollup
# ---------------------------------------------------------------------------

def test_aggregate_pointer_evidence_empty_list_is_not_vacuously_true():
    out = aggregate_pointer_evidence([])
    assert out == {
        "structural_valid": None,
        "target_resolved": False,
        "provenance_verified": None,
        "resolution_source": "not_applicable",
        # 62640241 — additive freshness rollup, same "not vacuously true" rule.
        "freshness_verified": None,
    }


def test_aggregate_pointer_evidence_all_pass():
    records = [
        {"structural_valid": True, "target_resolved": True,
         "provenance_verified": True, "resolution_source": "live_graph"},
        {"structural_valid": True, "target_resolved": True,
         "provenance_verified": None, "resolution_source": "not_applicable"},
    ]
    out = aggregate_pointer_evidence(records)
    assert out["structural_valid"] is True
    assert out["target_resolved"] is True
    assert out["provenance_verified"] is True  # no False anywhere, >=1 True
    assert out["resolution_source"] == "live_graph"


def test_aggregate_pointer_evidence_one_unresolved_fails_the_whole_item():
    records = [
        {"structural_valid": True, "target_resolved": True,
         "provenance_verified": True, "resolution_source": "live_graph"},
        {"structural_valid": True, "target_resolved": False,
         "provenance_verified": None, "resolution_source": "stale_snapshot"},
    ]
    out = aggregate_pointer_evidence(records)
    assert out["target_resolved"] is False  # NOT vacuously True
    assert out["resolution_source"] == "mixed"


def test_aggregate_pointer_evidence_provenance_false_dominates():
    records = [
        {"structural_valid": True, "target_resolved": True,
         "provenance_verified": None, "resolution_source": "not_applicable"},
        {"structural_valid": True, "target_resolved": True,
         "provenance_verified": False, "resolution_source": "not_applicable"},
    ]
    out = aggregate_pointer_evidence(records)
    assert out["provenance_verified"] is False


# ---------------------------------------------------------------------------
# db.is_item_claim_prospected — opt-in STRICT gate (target_resolved-aware)
# ---------------------------------------------------------------------------

def test_is_item_claim_prospected_default_unaffected_by_new_kwargs():
    """Every pre-existing call site (no strict/target_resolved kwargs) sees
    byte-for-byte identical results — the presence-only check is untouched."""
    item = {"touches_resources": ["file:x.py"]}
    assert db_module.is_item_claim_prospected(item, has_pointer_evidence=True) is True
    assert db_module.is_item_claim_prospected(item, has_pointer_evidence=False) is False


def test_is_item_claim_prospected_strict_false_target_resolved_is_ignored():
    """strict=False (the default) never consults target_resolved, even if a
    caller happens to pass it — mirrors the OFF-by-default opt-in contract."""
    item = {"touches_resources": ["file:x.py"]}
    assert db_module.is_item_claim_prospected(
        item, has_pointer_evidence=True, strict=False, target_resolved=False,
    ) is True


def test_is_item_claim_prospected_strict_true_blocks_unresolved_row():
    """THE fix: a row exists (has_pointer_evidence=True) but did NOT resolve
    (target_resolved=False) — under strict=True this must now FAIL the gate,
    not pass just because a row exists."""
    item = {"touches_resources": ["file:x.py"]}
    assert db_module.is_item_claim_prospected(
        item, has_pointer_evidence=True, strict=True, target_resolved=False,
    ) is False


def test_is_item_claim_prospected_strict_true_passes_when_resolved():
    item = {"touches_resources": ["file:x.py"]}
    assert db_module.is_item_claim_prospected(
        item, has_pointer_evidence=True, strict=True, target_resolved=True,
    ) is True


def test_is_item_claim_prospected_strict_true_target_resolved_none_falls_back():
    """A caller that opts into strict=True but has no resolution-aware signal
    (target_resolved=None — e.g. never annotated) is NOT punished: it falls
    back to the presence-only result, exactly like strict=False."""
    item = {"touches_resources": ["file:x.py"]}
    assert db_module.is_item_claim_prospected(
        item, has_pointer_evidence=True, strict=True, target_resolved=None,
    ) is True


def test_is_item_claim_prospected_strict_true_bypass_and_no_resources_still_win():
    """prospect_bypass and 'never a real prospecting candidate' still short-
    circuit True even under strict=True — strict only tightens the
    has_pointer_evidence branch, nothing else."""
    bypassed = {"touches_resources": ["file:x.py"], "prospect_bypass": True}
    assert db_module.is_item_claim_prospected(
        bypassed, has_pointer_evidence=False, strict=True, target_resolved=False,
    ) is True
    no_resources = {}
    assert db_module.is_item_claim_prospected(
        no_resources, has_pointer_evidence=False, strict=True, target_resolved=False,
    ) is True


# ---------------------------------------------------------------------------
# handoff._annotate_resolved_pointers — the end-to-end bug-fix demonstration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_annotate_resolved_pointers_distinguishes_presence_from_resolution(db):
    """THE confirmed bug, reproduced and fixed: a durable pointer row exists
    (pointer_provenance.satisfied stays True — presence-only, unchanged
    behavior) while the resolver could not find the target at all (default
    resolver, empty codebase_graph_entities snapshot in this test db) — the
    new pointer_resolution_status field must show target_resolved=False and
    strict_satisfied=False, making the mismatch explicit instead of hidden."""
    p = await db_module.create_project(db, "eb8b6894-presence-vs-resolution")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Fix the thing",
        touches_resources=["file:meridian/nonexistent_module.py"],
    )
    await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "code",
        [{"uri": "file:meridian/nonexistent_module.py",
          "selector": {"type": "symbol", "qualified_name": "totally.unindexed.symbol"}}],
    )
    out = await handoff_module._annotate_resolved_pointers(db, p["id"], [item])
    it = out[0]

    # Presence-only field: unchanged, still (incorrectly, by itself) "satisfied".
    assert it["pointer_provenance"]["satisfied"] is True

    # The new, resolution-aware companion tells the real story.
    status = it["pointer_resolution_status"]
    assert status["structural_valid"] is True
    assert status["target_resolved"] is False
    assert status["strict_satisfied"] is False

    # And the per-pointer record backing that rollup carries the same signal.
    rec = it["pointer_records"][0]
    assert rec["structural_valid"] is True
    assert rec["target_resolved"] is False


@pytest.mark.asyncio
async def test_annotate_resolved_pointers_resolution_status_present_even_with_zero_pointers(db):
    p = await db_module.create_project(db, "eb8b6894-zero-pointers")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Needs prospecting",
        touches_resources=["file:meridian/some_module.py"],
    )
    out = await handoff_module._annotate_resolved_pointers(db, p["id"], [item])
    it = out[0]
    assert it["pointer_resolution_status"]["target_resolved"] is False
    assert it["pointer_resolution_status"]["strict_satisfied"] is False


@pytest.mark.asyncio
async def test_annotate_resolved_pointers_fully_resolved_range_pointer_is_strict_satisfied(db):
    """A range pointer at a REAL, existing file resolves AND is strict_satisfied
    — the strict gate must not punish genuinely-good evidence."""
    p = await db_module.create_project(db, "eb8b6894-resolved-range")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Fix the migration guard",
        touches_resources=["file:meridian/db/migrations.py"],
    )
    await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "code",
        [{"uri": "meridian/db/migrations.py",
          "selector": {"type": "range", "start_line": 1, "end_line": 2}}],
    )
    out = await handoff_module._annotate_resolved_pointers(db, p["id"], [item])
    it = out[0]
    status = it["pointer_resolution_status"]
    assert status["target_resolved"] is True
    assert status["strict_satisfied"] is True


# ---------------------------------------------------------------------------
# eb8b6894 — resolution_source: default resolver tags the cached snapshot
# path explicitly (never presumed "live").
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_default_symbol_resolver_tags_stale_snapshot(db):
    p = await db_module.create_project(db, "eb8b6894-stale-snapshot-tag")
    await db_module.upsert_graph_entities(db, p["id"], [
        {"qualified_name": "auth.login_user", "file": "auth.py"},
    ])
    resolved = await resolve_pointer(
        db,
        {"source_type": "code", "targets": [
            {"uri": "auth.py", "selector": {"type": "symbol",
                                             "qualified_name": "auth.login_user"}},
        ]},
        project_id=p["id"],
    )
    target = resolved["targets"][0]
    assert target["resolved"] is True
    assert target["resolution_source"] == "stale_snapshot"


@pytest.mark.asyncio
async def test_symbol_resolver_stub_without_resolution_source_reports_unknown():
    """A caller-injected symbol_resolver that doesn't self-report a source
    (e.g. a bare test stub) must never be PRESUMED live."""
    async def _stub(_db, _pid, _q, _lim):
        return [{"qualified_name": "a.b", "file": "a.py"}]

    resolved = await resolve_pointer(
        None,
        {"source_type": "code", "targets": [
            {"uri": "a.py", "selector": {"type": "symbol", "qualified_name": "a.b"}},
        ]},
        project_id="p",
        symbol_resolver=_stub,
    )
    assert resolved["targets"][0]["resolution_source"] == "unknown"


@pytest.mark.asyncio
async def test_build_symbol_resolver_tags_live_graph_vs_stale_snapshot(monkeypatch):
    """prospect.build_symbol_resolver — the SAME live-vs-fallback distinction
    test_mcp_resolve_pointers_reaches_live_graph_via_tenant already proves
    exists at the resolution level, now surfaced explicitly as
    resolution_source rather than left implicit."""
    from meridian import prospect as prospect_module
    from meridian.routes import tunnel as tunnel_module

    async def _fake_call_tunnel(tid, name, args, **kw):
        if name == "codebase__search_graph":
            return {"content": [{"type": "text", "text":
                '{"results": [{"qualified_name": "meridian.server.mcp_tools_doc", '
                '"file": "meridian/server.py"}]}'}]}
        raise AssertionError(f"unexpected tunnel tool: {name}")

    monkeypatch.setattr(tunnel_module, "call_tunnel_tool", _fake_call_tunnel)
    monkeypatch.setattr(tunnel_module, "has_active_tunnel", lambda tid: True)

    resolver = prospect_module.build_symbol_resolver(tenant={"id": "tenant-live"})
    hits = await resolver(None, "proj", "meridian.server.mcp_tools_doc", 5)
    assert hits
    assert hits[0]["resolution_source"] == "live_graph"


@pytest.mark.asyncio
async def test_build_symbol_resolver_snapshot_fallback_tags_stale_snapshot(db, monkeypatch):
    """No active tunnel at all -> falls all the way through to the cached
    codebase_graph_entities snapshot; that fallback must be tagged too."""
    from meridian import prospect as prospect_module
    from meridian.routes import tunnel as tunnel_module

    monkeypatch.setattr(tunnel_module, "has_active_tunnel", lambda tid: False)
    p = await db_module.create_project(db, "eb8b6894-resolver-fallback")
    await db_module.upsert_graph_entities(db, p["id"], [
        {"qualified_name": "billing.charge_customer", "file": "billing.py"},
    ])
    resolver = prospect_module.build_symbol_resolver(tenant={"id": "tenant-none"})
    hits = await resolver(db, p["id"], "billing.charge_customer", 5)
    assert hits
    assert hits[0]["resolution_source"] == "stale_snapshot"


# ---------------------------------------------------------------------------
# eb8b6894 — strict_pointer_evidence opt-in: excludes an item whose durable
# pointer never resolved from the claimable /goal batch, WITHOUT affecting
# the default (non-strict) caller at all.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_handoff_strict_pointer_evidence_excludes_unresolved_item(db, tmp_path):
    p = await db_module.create_project(db, "eb8b6894-strict-gate-goal")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Fix the thing",
        touches_resources=["file:meridian/nonexistent_module.py"],
    )
    await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "code",
        [{"uri": "file:meridian/nonexistent_module.py",
          "selector": {"type": "symbol", "qualified_name": "totally.unindexed.symbol"}}],
    )

    # Default (non-strict): presence alone satisfies the gate — item is
    # claimable, exactly as before this sprint item existed.
    _, default_content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), mode="goal", skip_ai_summary=True,
    )
    assert item["id"] in default_content
    assert "<excluded_unprospected" not in default_content

    # strict_pointer_evidence=True: the SAME item is now excluded, because
    # its durable pointer never actually resolved.
    _, strict_content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), mode="goal", skip_ai_summary=True,
        strict_pointer_evidence=True,
    )
    assert "<excluded_unprospected" in strict_content
    assert item["id"] in strict_content  # named in the exclusion tag itself


def test_build_quick_start_goal_default_ignores_pointer_resolution_status():
    """Direct unit-level proof that omitting strict_pointer_evidence (every
    pre-existing caller) is unaffected by an item's pointer_resolution_status
    even when that status says target_resolved=False."""
    items = [{
        "id": "item-1", "title": "do the thing",
        "touches_resources": ["file:x.py"],
        "pointer_resolution_status": {"target_resolved": False, "strict_satisfied": False},
    }]
    out = handoff_module._build_quick_start_goal(items, pointer_evidence_ids={"item-1"})
    assert "<excluded_unprospected" not in out
    assert "item-1" in out


def test_build_quick_start_goal_strict_pointer_evidence_excludes_when_unresolved():
    items = [{
        "id": "item-1", "title": "do the thing",
        "touches_resources": ["file:x.py"],
        "pointer_resolution_status": {"target_resolved": False, "strict_satisfied": False},
    }]
    out = handoff_module._build_quick_start_goal(
        items, pointer_evidence_ids={"item-1"}, strict_pointer_evidence=True,
    )
    assert "<excluded_unprospected" in out
    assert "item-1" in out


# ===========================================================================
# 3b3020ac — execution-manifest-backed readiness
# (verify_execution_manifest_target_readiness), a thin adapter layered on
# top of verify_target_readiness that consumes
# meridian.executor_contract.aggregate_worker_completions()'s fail-closed
# aggregation (duck-typed here; this module never imports executor_contract
# — see the adapter's own module-section docstring for why).
# ===========================================================================

import hashlib as _hashlib  # noqa: E402

from meridian.pointers import verify_execution_manifest_target_readiness  # noqa: E402


def _ok_aggregation(worker_records):
    return {"ok": True, "status": "complete", "is_full_production": True, "worker_records": worker_records}


class TestVerifyExecutionManifestTargetReadiness:
    @pytest.mark.asyncio
    async def test_disk_level_failure_short_circuits_before_manifest_check(self, tmp_path):
        """A target that fails verify_target_readiness's own disk check is
        never rescued by a manifest, no matter what the aggregation says."""
        missing = tmp_path / "nope.png"
        out = await verify_execution_manifest_target_readiness(
            {"uri": str(missing), "target_kind": "existing"},
            _ok_aggregation({}),
        )
        assert out["ready"] is False
        assert out["status"] == "missing"
        assert out["manifest_verified"] is False

    @pytest.mark.asyncio
    async def test_missing_or_not_ok_aggregation_refuses_directory_presence_alone(self, tmp_path):
        """Directory presence alone (a real file on disk) must never be
        treated as sufficient — an absent or failed aggregation downgrades
        an otherwise-ready target to not ready."""
        real = tmp_path / "out.png"
        real.write_bytes(b"hello world")
        target = {"uri": str(real), "target_kind": "existing"}

        out_none = await verify_execution_manifest_target_readiness(target, None)
        assert out_none["ready"] is False
        assert out_none["manifest_verified"] is False

        out_failed = await verify_execution_manifest_target_readiness(
            target, {"ok": False, "status": "failed", "worker_records": {}},
        )
        assert out_failed["ready"] is False
        assert out_failed["manifest_verified"] is False

    @pytest.mark.asyncio
    async def test_matching_manifest_hash_verifies_ready(self, tmp_path):
        real = tmp_path / "out.png"
        real.write_bytes(b"hello world")
        content_hash = _hashlib.sha256(b"hello world").hexdigest()
        aggregation = _ok_aggregation({"w1": {"output_hashes": {str(real): content_hash}}})

        out = await verify_execution_manifest_target_readiness(
            {"uri": str(real), "target_kind": "existing"}, aggregation,
        )
        assert out["ready"] is True
        assert out["manifest_verified"] is True
        assert out["manifest_reason"] is None

    @pytest.mark.asyncio
    async def test_path_not_recorded_in_aggregation_refuses(self, tmp_path):
        real = tmp_path / "out.png"
        real.write_bytes(b"hello world")
        aggregation = _ok_aggregation({"w1": {"output_hashes": {}}})

        out = await verify_execution_manifest_target_readiness(
            {"uri": str(real), "target_kind": "existing"}, aggregation,
        )
        assert out["ready"] is False
        assert out["manifest_verified"] is False
        assert "not among" in out["manifest_reason"]

    @pytest.mark.asyncio
    async def test_hash_mismatch_between_disk_and_manifest_refuses(self, tmp_path):
        """The file on disk has since changed — its current content hash no
        longer matches what the manifest-backed aggregation recorded."""
        real = tmp_path / "out.png"
        real.write_bytes(b"hello world")
        stale_hash = _hashlib.sha256(b"a completely different original content").hexdigest()
        aggregation = _ok_aggregation({"w1": {"output_hashes": {str(real): stale_hash}}})

        out = await verify_execution_manifest_target_readiness(
            {"uri": str(real), "target_kind": "existing"}, aggregation,
        )
        assert out["ready"] is False
        assert out["manifest_verified"] is False
        assert "does not match" in out["manifest_reason"]

    @pytest.mark.asyncio
    async def test_injected_hash_file_seam_is_used(self, tmp_path):
        real = tmp_path / "out.png"
        real.write_bytes(b"hello world")
        aggregation = _ok_aggregation({"w1": {"output_hashes": {str(real): "stub-hash"}}})

        out = await verify_execution_manifest_target_readiness(
            {"uri": str(real), "target_kind": "existing"}, aggregation,
            hash_file=lambda _p: "stub-hash",
        )
        assert out["ready"] is True
        assert out["manifest_verified"] is True


# ---------------------------------------------------------------------------
# S5 — typed external pointer targets + freshness proofs (62640241)
#
# Covers the FEAT: directory / git / remote_fs / artifact selector types,
# text_quote's additive canonical_url/retrieval_hash fields, the universal
# opt-in `freshness` proof on any target, resolve_pointer's live
# freshness_state recomputation, the build_typed_pointer_record /
# aggregate_pointer_evidence freshness rollups, and strict_freshness_gate.
# ---------------------------------------------------------------------------

import hashlib as _hashlib_s5
import os as _os_s5
import shutil as _shutil_s5
import subprocess as _subprocess_s5

from meridian.pointers import (  # noqa: E402
    build_typed_pointer_record,
    aggregate_pointer_evidence,
    strict_freshness_gate,
)


# --- validation: directory ---------------------------------------------------

def test_validate_directory_selector_round_trips():
    ptr = validate_pointer({
        "source_type": "infra",
        "targets": [{"uri": "some/dir", "selector": {
            "type": "directory", "root": "some/dir",
            "include": ["*.py"], "exclude": ["**/test_*.py"],
            "manifest_id": "m1", "snapshot_id": "s1",
        }}],
    })
    sel = ptr["targets"][0]["selector"]
    assert sel == {
        "type": "directory", "root": "some/dir",
        "include": ["*.py"], "exclude": ["**/test_*.py"],
        "manifest_id": "m1", "snapshot_id": "s1",
    }


@pytest.mark.parametrize("bad_sel", [
    {"type": "directory"},                                    # missing root
    {"type": "directory", "root": "   "},                     # blank root
    {"type": "directory", "root": "d", "include": []},        # empty include list
    {"type": "directory", "root": "d", "include": "*.py"},    # not a list
    {"type": "directory", "root": "d", "include": [5]},       # non-str entry
    {"type": "directory", "root": "d", "manifest_id": ""},    # blank manifest_id
])
def test_validate_directory_rejects_malformed(bad_sel):
    with pytest.raises(PointerValidationError):
        validate_pointer({"source_type": "infra", "targets": [{"uri": "u", "selector": bad_sel}]})


# --- validation: git ----------------------------------------------------------

def test_validate_git_selector_ref_or_commit_required():
    ptr = validate_pointer({
        "source_type": "code",
        "targets": [{"uri": "repo", "selector": {
            "type": "git", "repository": "/path/to/repo", "commit": "abc123",
        }}],
    })
    assert ptr["targets"][0]["selector"] == {
        "type": "git", "repository": "/path/to/repo", "commit": "abc123",
    }

    ptr2 = validate_pointer({
        "source_type": "code",
        "targets": [{"uri": "repo", "selector": {
            "type": "git", "repository": "/path/to/repo", "ref": "main", "path": "src/x.py",
        }}],
    })
    assert ptr2["targets"][0]["selector"]["ref"] == "main"
    assert ptr2["targets"][0]["selector"]["path"] == "src/x.py"


@pytest.mark.parametrize("bad_sel", [
    {"type": "git"},                                                # missing repository
    {"type": "git", "repository": "  "},                            # blank repository
    {"type": "git", "repository": "r"},                             # neither ref nor commit
    {"type": "git", "repository": "r", "ref": ""},                  # blank ref
    {"type": "git", "repository": "r", "commit": "  "},             # blank commit
    {"type": "git", "repository": "r", "ref": "main", "path": ""},  # blank path
])
def test_validate_git_rejects_malformed(bad_sel):
    with pytest.raises(PointerValidationError):
        validate_pointer({"source_type": "code", "targets": [{"uri": "u", "selector": bad_sel}]})


def test_validate_git_range_within_path_uses_subselector():
    """Acceptance-relevant design choice: a line range within a git target's
    ``path`` reuses the EXISTING subSelector mechanism rather than a
    duplicated field."""
    ptr = validate_pointer({
        "source_type": "code",
        "targets": [{"uri": "repo", "selector": {
            "type": "git", "repository": "r", "commit": "abc", "path": "a.py",
            "subSelector": {"type": "range", "start_line": 1, "end_line": 4},
        }}],
    })
    sub = ptr["targets"][0]["selector"]["subSelector"]
    assert sub == {"type": "range", "start_line": 1, "end_line": 4}


# --- validation: remote_fs -----------------------------------------------------

def test_validate_remote_fs_selector_round_trips():
    ptr = validate_pointer({
        "source_type": "infra",
        "targets": [{"uri": "remote://host/x", "selector": {
            "type": "remote_fs", "host_id": "h1", "filesystem_slot": "Filesystem",
            "path": "/data/x", "lease_id": "lease-1", "session_id": "sess-1",
            "snapshot_id": "snap-1",
        }}],
    })
    sel = ptr["targets"][0]["selector"]
    assert sel["host_id"] == "h1" and sel["filesystem_slot"] == "Filesystem"
    assert sel["path"] == "/data/x"
    assert sel["lease_id"] == "lease-1" and sel["session_id"] == "sess-1"
    assert sel["snapshot_id"] == "snap-1"


@pytest.mark.parametrize("bad_sel", [
    {"type": "remote_fs"},                                            # missing host_id
    {"type": "remote_fs", "host_id": "h"},                            # missing filesystem_slot
    {"type": "remote_fs", "host_id": "h", "filesystem_slot": "f"},    # missing path
    {"type": "remote_fs", "host_id": "h", "filesystem_slot": "f", "path": "p", "lease_id": ""},
])
def test_validate_remote_fs_rejects_malformed(bad_sel):
    with pytest.raises(PointerValidationError):
        validate_pointer({"source_type": "infra", "targets": [{"uri": "u", "selector": bad_sel}]})


# --- validation: artifact -------------------------------------------------------

def test_validate_artifact_selector_round_trips():
    ptr = validate_pointer({
        "source_type": "experiment",
        "targets": [{"uri": "artifact://x", "selector": {
            "type": "artifact", "manifest_uri": "out/manifest.json",
            "fingerprint": "sha256:abc", "run_id": "run-1", "item_id": "item-1",
            "provenance_id": "prov-1",
        }}],
    })
    sel = ptr["targets"][0]["selector"]
    assert sel["manifest_uri"] == "out/manifest.json"
    assert sel["fingerprint"] == "sha256:abc"
    assert sel["run_id"] == "run-1" and sel["item_id"] == "item-1"
    assert sel["provenance_id"] == "prov-1"


@pytest.mark.parametrize("bad_sel", [
    {"type": "artifact"},                                      # missing manifest_uri
    {"type": "artifact", "manifest_uri": "  "},                # blank
    {"type": "artifact", "manifest_uri": "m", "fingerprint": ""},
])
def test_validate_artifact_rejects_malformed(bad_sel):
    with pytest.raises(PointerValidationError):
        validate_pointer({"source_type": "experiment", "targets": [{"uri": "u", "selector": bad_sel}]})


# --- validation: text_quote additive web fields ---------------------------------

def test_validate_text_quote_canonical_url_and_retrieval_hash_additive():
    ptr = validate_pointer({
        "source_type": "web",
        "targets": [{"uri": "https://example.com/a", "selector": {
            "type": "text_quote", "exact": "the cited passage",
            "canonical_url": "https://example.com/canonical/a",
            "retrieval_hash": "sha256:deadbeef",
        }}],
    })
    sel = ptr["targets"][0]["selector"]
    assert sel["canonical_url"] == "https://example.com/canonical/a"
    assert sel["retrieval_hash"] == "sha256:deadbeef"


# --- validation: universal freshness proof --------------------------------------

def test_validate_freshness_proof_round_trips_and_defaults_unknown_state():
    ptr = validate_pointer({
        "source_type": "infra",
        "targets": [{"uri": "d", "selector": {"type": "directory", "root": "d"},
                     "freshness": {"content_hash": "abc", "captured_at": "2026-08-08T00:00:00Z"}}],
    })
    fresh = ptr["targets"][0]["freshness"]
    assert fresh["content_hash"] == "abc"
    assert fresh["captured_at"] == "2026-08-08T00:00:00Z"
    assert fresh["state"] == "unknown"  # default when omitted


def test_validate_freshness_explicit_state_and_all_states_accepted():
    for state in ("current", "stale", "unknown", "unavailable", "ambiguous"):
        ptr = validate_pointer({
            "source_type": "infra",
            "targets": [{"uri": "d", "selector": {"type": "directory", "root": "d"},
                         "freshness": {"state": state}}],
        })
        assert ptr["targets"][0]["freshness"]["state"] == state


@pytest.mark.parametrize("bad_freshness", [
    "not-a-dict",
    {"state": "not-a-real-state"},
    {"content_hash": 5},
    {"content_hash": ""},
    {"source_revision": None, "captured_at": 5},
])
def test_validate_freshness_rejects_malformed(bad_freshness):
    with pytest.raises(PointerValidationError):
        validate_pointer({
            "source_type": "infra",
            "targets": [{"uri": "d", "selector": {"type": "directory", "root": "d"},
                         "freshness": bad_freshness}],
        })


def test_validate_pointer_omits_freshness_key_when_not_supplied():
    ptr = validate_pointer({
        "source_type": "infra",
        "targets": [{"uri": "d", "selector": {"type": "directory", "root": "d"}}],
    })
    assert "freshness" not in ptr["targets"][0]


# --- resolution: directory ------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_directory_default_resolver_real_filesystem(tmp_path):
    (tmp_path / "a.py").write_text("print('a')")
    (tmp_path / "b.py").write_text("print('b')")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.py").write_text("print('c')")
    (tmp_path / "notes.txt").write_text("hi")

    ptr = {"source_type": "infra", "targets": [{"uri": str(tmp_path), "selector": {
        "type": "directory", "root": str(tmp_path), "include": ["*.py"],
    }}]}
    out = (await resolve_pointer(None, ptr))["targets"][0]
    assert out["resolved"] is True
    manifest = out["manifest"]
    assert manifest["entry_count"] == 3
    assert all(e.endswith(".py") for e in manifest["entries"])
    assert manifest["manifest_hash"]
    # freshness: no declared proof on the target -> "unknown"
    assert out["freshness_state"] == "unknown"


@pytest.mark.asyncio
async def test_resolve_directory_missing_root_is_unresolved():
    ptr = {"source_type": "infra", "targets": [{"uri": "nope", "selector": {
        "type": "directory", "root": "C:/definitely/not/a/real/dir/62640241",
    }}]}
    out = (await resolve_pointer(None, ptr))["targets"][0]
    assert out["resolved"] is False
    assert out["freshness_state"] == "unavailable"


@pytest.mark.asyncio
async def test_resolve_directory_resolver_exception_is_guarded():
    async def boom(_selector):
        raise RuntimeError("disk error")
    ptr = {"source_type": "infra", "targets": [{"uri": "d", "selector": {
        "type": "directory", "root": "d",
    }}]}
    out = (await resolve_pointer(None, ptr, directory_resolver=boom))["targets"][0]
    assert out["resolved"] is False


@pytest.mark.asyncio
async def test_resolve_directory_freshness_current_and_stale(tmp_path):
    (tmp_path / "a.py").write_text("x")
    selector = {"type": "directory", "root": str(tmp_path), "include": ["*.py"]}
    live = (await resolve_pointer(
        None, {"source_type": "infra", "targets": [{"uri": str(tmp_path), "selector": selector}]},
    ))["targets"][0]
    live_hash = live["manifest"]["manifest_hash"]

    ptr_current = {"source_type": "infra", "targets": [{
        "uri": str(tmp_path), "selector": selector,
        "freshness": {"content_hash": live_hash},
    }]}
    out_current = (await resolve_pointer(None, ptr_current))["targets"][0]
    assert out_current["freshness_state"] == "current"

    ptr_stale = {"source_type": "infra", "targets": [{
        "uri": str(tmp_path), "selector": selector,
        "freshness": {"content_hash": "not-the-real-hash"},
    }]}
    out_stale = (await resolve_pointer(None, ptr_stale))["targets"][0]
    assert out_stale["freshness_state"] == "stale"


# --- resolution: git -------------------------------------------------------------

def _init_git_repo_s5(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**_os_s5.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com"}

    def run(*args):
        return _subprocess_s5.run(
            ["git", *args], cwd=str(repo), capture_output=True, text=True,
            check=True, env=env,
        )

    run("init")
    run("config", "user.email", "t@t.com")
    run("config", "user.name", "t")
    (repo / "f.txt").write_text("v1")
    run("add", "f.txt")
    run("commit", "-m", "v1")
    sha1 = run("rev-parse", "HEAD").stdout.strip()
    return repo, sha1


_HAS_GIT_S5 = _shutil_s5.which("git") is not None


@pytest.mark.skipif(not _HAS_GIT_S5, reason="git CLI not available")
@pytest.mark.asyncio
async def test_resolve_git_default_resolver_reachable_commit(tmp_path):
    repo, sha1 = _init_git_repo_s5(tmp_path)
    ptr = {"source_type": "code", "targets": [{"uri": str(repo), "selector": {
        "type": "git", "repository": str(repo), "commit": sha1,
    }}]}
    out = (await resolve_pointer(None, ptr))["targets"][0]
    assert out["resolved"] is True
    assert out["head"] == sha1
    assert out["requested_sha"] == sha1
    assert out["freshness_state"] == "unknown"  # no declared proof


@pytest.mark.skipif(not _HAS_GIT_S5, reason="git CLI not available")
@pytest.mark.asyncio
async def test_resolve_git_unreachable_ref_is_unresolved_with_reason(tmp_path):
    repo, _sha1 = _init_git_repo_s5(tmp_path)
    ptr = {"source_type": "code", "targets": [{"uri": str(repo), "selector": {
        "type": "git", "repository": str(repo), "ref": "totally-not-a-real-branch",
    }}]}
    out = (await resolve_pointer(None, ptr))["targets"][0]
    assert out["resolved"] is False
    assert "not reachable" in out["reason"]
    assert out["freshness_state"] == "unavailable"


@pytest.mark.skipif(not _HAS_GIT_S5, reason="git CLI not available")
@pytest.mark.asyncio
async def test_resolve_git_freshness_current_and_stale_by_source_revision(tmp_path):
    repo, sha1 = _init_git_repo_s5(tmp_path)
    selector = {"type": "git", "repository": str(repo), "commit": sha1}

    ptr_current = {"source_type": "code", "targets": [{
        "uri": str(repo), "selector": selector, "freshness": {"source_revision": sha1},
    }]}
    out_current = (await resolve_pointer(None, ptr_current))["targets"][0]
    assert out_current["freshness_state"] == "current"

    ptr_stale = {"source_type": "code", "targets": [{
        "uri": str(repo), "selector": selector, "freshness": {"source_revision": "0" * 40},
    }]}
    out_stale = (await resolve_pointer(None, ptr_stale))["targets"][0]
    assert out_stale["freshness_state"] == "stale"


@pytest.mark.asyncio
async def test_resolve_git_non_local_repository_is_unresolved():
    ptr = {"source_type": "code", "targets": [{"uri": "u", "selector": {
        "type": "git", "repository": "https://github.com/example/repo.git", "ref": "main",
    }}]}
    out = (await resolve_pointer(None, ptr))["targets"][0]
    assert out["resolved"] is False


@pytest.mark.asyncio
async def test_resolve_git_resolver_exception_is_guarded():
    async def boom(_selector):
        raise RuntimeError("git binary missing")
    ptr = {"source_type": "code", "targets": [{"uri": "u", "selector": {
        "type": "git", "repository": "r", "commit": "abc",
    }}]}
    out = (await resolve_pointer(None, ptr, git_resolver=boom))["targets"][0]
    assert out["resolved"] is False


# --- resolution: remote_fs (no core-local default) --------------------------------

@pytest.mark.asyncio
async def test_resolve_remote_fs_without_injected_resolver_is_unresolved():
    ptr = {"source_type": "infra", "targets": [{"uri": "remote://h/p", "selector": {
        "type": "remote_fs", "host_id": "h1", "filesystem_slot": "Filesystem", "path": "/p",
    }}]}
    out = (await resolve_pointer(None, ptr))["targets"][0]
    assert out["resolved"] is False
    assert "no remote filesystem resolver" in out["reason"]
    assert out["freshness_state"] == "unavailable"


@pytest.mark.asyncio
async def test_resolve_remote_fs_with_injected_resolver_resolves():
    async def fake_remote_fs(selector):
        assert selector["host_id"] == "h1"
        return {"content_hash": "live-hash-1", "size": 42}

    ptr = {"source_type": "infra", "targets": [{"uri": "remote://h/p", "selector": {
        "type": "remote_fs", "host_id": "h1", "filesystem_slot": "Filesystem", "path": "/p",
    }, "freshness": {"content_hash": "live-hash-1"}}]}
    out = (await resolve_pointer(None, ptr, remote_fs_resolver=fake_remote_fs))["targets"][0]
    assert out["resolved"] is True
    assert out["manifest"]["content_hash"] == "live-hash-1"
    assert out["freshness_state"] == "current"


@pytest.mark.asyncio
async def test_resolve_remote_fs_injected_resolver_exception_is_guarded():
    async def boom(_selector):
        raise RuntimeError("tunnel down")
    ptr = {"source_type": "infra", "targets": [{"uri": "u", "selector": {
        "type": "remote_fs", "host_id": "h", "filesystem_slot": "f", "path": "p",
    }}]}
    out = (await resolve_pointer(None, ptr, remote_fs_resolver=boom))["targets"][0]
    assert out["resolved"] is False


@pytest.mark.asyncio
async def test_resolve_remote_fs_no_leakage_across_two_projects():
    """62640241 acceptance criterion 5, at the pure-function layer this
    module owns: resolve_pointer keeps NO module-global cache — every
    resolver seam is per-call — so two independently-injected resolvers
    for two different "projects" sharing the SAME host_id never share
    state or answers. (A live, DB-backed two-tenant test belongs at the
    tunnel/routes layer, out of scope for this pure module.)"""
    async def resolver_for_project(pid):
        async def _r(selector):
            return {"content_hash": f"{pid}-hash", "seen_host": selector["host_id"]}
        return _r

    selector = {"type": "remote_fs", "host_id": "shared-host",
                "filesystem_slot": "f", "path": "/x"}
    ptr_p1 = {"source_type": "infra", "targets": [{"uri": "u", "selector": selector}]}
    ptr_p2 = {"source_type": "infra", "targets": [{"uri": "u", "selector": selector}]}

    out1 = (await resolve_pointer(
        None, ptr_p1, project_id="project-1",
        remote_fs_resolver=await resolver_for_project("project-1"),
    ))["targets"][0]
    out2 = (await resolve_pointer(
        None, ptr_p2, project_id="project-2",
        remote_fs_resolver=await resolver_for_project("project-2"),
    ))["targets"][0]

    assert out1["manifest"]["content_hash"] == "project-1-hash"
    assert out2["manifest"]["content_hash"] == "project-2-hash"


# --- resolution: artifact ---------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_artifact_default_resolver_hashes_local_file(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(b'{"ok": true}')
    expected = _hashlib_s5.sha256(b'{"ok": true}').hexdigest()

    ptr = {"source_type": "experiment", "targets": [{"uri": str(manifest), "selector": {
        "type": "artifact", "manifest_uri": str(manifest),
    }}]}
    out = (await resolve_pointer(None, ptr))["targets"][0]
    assert out["resolved"] is True
    assert out["manifest"]["content_hash"] == expected


@pytest.mark.asyncio
async def test_resolve_artifact_missing_file_is_unresolved(tmp_path):
    missing = tmp_path / "does-not-exist.json"
    ptr = {"source_type": "experiment", "targets": [{"uri": str(missing), "selector": {
        "type": "artifact", "manifest_uri": str(missing),
    }}]}
    out = (await resolve_pointer(None, ptr))["targets"][0]
    assert out["resolved"] is False


@pytest.mark.asyncio
async def test_resolve_artifact_freshness_current_and_stale(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(b"payload-v1")
    real_hash = _hashlib_s5.sha256(b"payload-v1").hexdigest()

    selector = {"type": "artifact", "manifest_uri": str(manifest)}
    ptr_current = {"source_type": "experiment", "targets": [{
        "uri": str(manifest), "selector": selector, "freshness": {"content_hash": real_hash},
    }]}
    out_current = (await resolve_pointer(None, ptr_current))["targets"][0]
    assert out_current["freshness_state"] == "current"

    ptr_stale = {"source_type": "experiment", "targets": [{
        "uri": str(manifest), "selector": selector, "freshness": {"content_hash": "stale-hash"},
    }]}
    out_stale = (await resolve_pointer(None, ptr_stale))["targets"][0]
    assert out_stale["freshness_state"] == "stale"


@pytest.mark.asyncio
async def test_resolve_artifact_resolver_exception_is_guarded():
    async def boom(_selector):
        raise RuntimeError("hash read failed")
    ptr = {"source_type": "experiment", "targets": [{"uri": "u", "selector": {
        "type": "artifact", "manifest_uri": "m",
    }}]}
    out = (await resolve_pointer(None, ptr, artifact_resolver=boom))["targets"][0]
    assert out["resolved"] is False


# --- resolution: text_quote freshness_state reuses drift ---------------------------

@pytest.mark.asyncio
async def test_resolve_text_quote_freshness_state_mirrors_drift():
    ptr = {"source_type": "web", "targets": [{"uri": "https://x/a", "selector": {
        "type": "text_quote", "exact": "the cited passage",
    }}]}

    async def present(_uri): return "... the cited passage lives here ..."
    hit = (await resolve_pointer(None, ptr, web_fetcher=present))["targets"][0]
    assert hit["freshness_state"] == "current"

    async def changed(_uri): return "totally different content now"
    drift = (await resolve_pointer(None, ptr, web_fetcher=changed))["targets"][0]
    assert drift["freshness_state"] == "stale"

    async def nothing(_uri): return None
    n = (await resolve_pointer(None, ptr, web_fetcher=nothing))["targets"][0]
    assert n["freshness_state"] == "unavailable"


# --- freshness concept is a no-op for the six original selector types --------------

@pytest.mark.asyncio
async def test_freshness_state_not_applicable_for_original_selector_types():
    ptr = {"source_type": "code", "targets": [
        {"uri": "a.py", "selector": {"type": "range", "start_line": 1, "end_line": 2}},
        {"uri": "a.py", "selector": {"type": "symbol", "qualified_name": "a.b"}},
        {"uri": "u", "selector": {"type": "zotero_key", "key": "K1"}},
    ]}
    async def sym_resolver(_db, _pid, _q, _lim): return []
    async def cite_resolver(_ref): return None
    out = await resolve_pointer(
        None, ptr, symbol_resolver=sym_resolver, citation_resolver=cite_resolver,
    )
    for t in out["targets"]:
        assert "freshness_state" not in t


# --- subSelector threads new resolver kwargs through --------------------------------

@pytest.mark.asyncio
async def test_subselector_directory_resolves_with_new_resolver_kwargs(tmp_path):
    (tmp_path / "a.py").write_text("x")
    ptr = {"source_type": "infra", "targets": [{
        "uri": "outer", "selector": {
            "type": "finding_id", "id": "f1",
            "subSelector": {"type": "directory", "root": str(tmp_path)},
        },
    }]}
    async def finder(_id): return {"id": "f1", "title": "t", "body": "b"}
    out = (await resolve_pointer(None, ptr, finding_resolver=finder))["targets"][0]
    assert out["subResolved"]["resolved"] is True


# --- typed record + rollup integration -----------------------------------------------

@pytest.mark.asyncio
async def test_typed_record_carries_freshness_state_and_declared_proof(tmp_path):
    (tmp_path / "a.py").write_text("x")
    selector = {"type": "directory", "root": str(tmp_path), "include": ["*.py"]}
    stored = {"id": "ptr-1", "source_type": "infra", "targets": [
        {"uri": str(tmp_path), "selector": selector, "target_kind": "existing",
         "freshness": {"content_hash": "stale-value", "state": "unknown"}},
    ]}
    resolved = await resolve_pointer(None, stored)
    record = build_typed_pointer_record(stored, resolved)
    target_entry = record["targets"][0]
    assert target_entry["freshness"]["content_hash"] == "stale-value"
    assert target_entry["freshness_state"] == "stale"
    assert record["freshness_verified"] is False
    assert "not current" in record["freshness_reason"]


@pytest.mark.asyncio
async def test_typed_record_freshness_verified_true_when_current(tmp_path):
    (tmp_path / "a.py").write_text("x")
    selector = {"type": "directory", "root": str(tmp_path), "include": ["*.py"]}
    live = (await resolve_pointer(
        None, {"source_type": "infra", "targets": [{"uri": str(tmp_path), "selector": selector}]},
    ))["targets"][0]
    live_hash = live["manifest"]["manifest_hash"]

    stored = {"id": "ptr-2", "source_type": "infra", "targets": [
        {"uri": str(tmp_path), "selector": selector, "target_kind": "existing",
         "freshness": {"content_hash": live_hash}},
    ]}
    resolved = await resolve_pointer(None, stored)
    record = build_typed_pointer_record(stored, resolved)
    assert record["targets"][0]["freshness_state"] == "current"
    assert record["freshness_verified"] is True
    assert "freshness_reason" not in record


def test_typed_record_freshness_verified_none_when_not_applicable():
    stored = {"id": "ptr-3", "source_type": "code", "targets": [
        {"uri": "a.py", "selector": {"type": "range", "start_line": 1, "end_line": 2},
         "target_kind": "existing"},
    ]}
    resolved = {"source_type": "code", "targets": [
        {"resolved": True, "selector_type": "range", "uri": "a.py", "range": {}},
    ]}
    record = build_typed_pointer_record(stored, resolved)
    assert "freshness_state" not in record["targets"][0]
    assert record["freshness_verified"] is None


def test_aggregate_pointer_evidence_freshness_rollup():
    fresh_ok = {"structural_valid": True, "target_resolved": True,
                "provenance_verified": None, "resolution_source": "not_applicable",
                "freshness_verified": True}
    fresh_bad = {"structural_valid": True, "target_resolved": True,
                 "provenance_verified": None, "resolution_source": "not_applicable",
                 "freshness_verified": False}
    fresh_na = {"structural_valid": True, "target_resolved": True,
                "provenance_verified": None, "resolution_source": "not_applicable",
                "freshness_verified": None}

    assert aggregate_pointer_evidence([fresh_ok])["freshness_verified"] is True
    assert aggregate_pointer_evidence([fresh_ok, fresh_bad])["freshness_verified"] is False
    assert aggregate_pointer_evidence([fresh_na])["freshness_verified"] is None
    assert aggregate_pointer_evidence([])["freshness_verified"] is None


# --- strict_freshness_gate -----------------------------------------------------------

def test_strict_freshness_gate_passes_when_all_current_or_not_applicable():
    records = [
        {"id": "p1", "source_type": "infra", "targets": [
            {"uri": "d", "freshness_state": "current"},
            {"uri": "a.py"},  # no freshness concept — never blocking
        ]},
    ]
    ok, reasons = strict_freshness_gate(records)
    assert ok is True
    assert reasons == []


def test_strict_freshness_gate_empty_records_passes():
    ok, reasons = strict_freshness_gate([])
    assert ok is True and reasons == []


@pytest.mark.parametrize("state", ["stale", "unknown", "unavailable", "ambiguous"])
def test_strict_freshness_gate_blocks_on_each_bad_state(state):
    records = [
        {"id": "p1", "source_type": "infra", "targets": [
            {"uri": "target-uri", "freshness_state": state},
        ]},
    ]
    ok, reasons = strict_freshness_gate(records)
    assert ok is False
    assert len(reasons) == 1
    assert "target-uri" in reasons[0]
    assert state in reasons[0]
    assert "p1" in reasons[0]


def test_strict_freshness_gate_reports_one_reason_per_blocking_target():
    records = [
        {"id": "p1", "source_type": "infra", "targets": [
            {"uri": "a", "freshness_state": "stale"},
            {"uri": "b", "freshness_state": "current"},
        ]},
        {"source_type": "code", "targets": [
            {"uri": "c", "freshness_state": "unknown"},
        ]},
    ]
    ok, reasons = strict_freshness_gate(records)
    assert ok is False
    assert len(reasons) == 2


def test_strict_freshness_gate_ignores_malformed_entries():
    ok, reasons = strict_freshness_gate([None, {"targets": "not-a-list"}, {"targets": [None]}])
    assert ok is True and reasons == []


# --- tool-manifest parity: the new selector types are advertised ---------------------

def test_add_sprint_item_pointer_schema_advertises_new_selector_types():
    """62640241 — the notes flagged that text_quote/finding_id existed
    internally but weren't consistently advertised in the public
    add_sprint_item_pointer schema; the new directory/git/remote_fs/artifact
    types + the freshness proof must not repeat that gap."""
    from meridian.mcp_tools import _MCP_TOOLS_LIST
    by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    tool = by_name["add_sprint_item_pointer"]
    haystack = tool["description"] + tool["inputSchema"]["properties"]["targets"]["description"]
    for token in (
        "directory", "git", "remote_fs", "artifact", "text_quote", "finding_id",
        "freshness", "canonical_url", "retrieval_hash",
    ):
        assert token in haystack, f"{token!r} not advertised in add_sprint_item_pointer schema"


def test_resolve_sprint_item_pointers_schema_mentions_freshness_state():
    from meridian.mcp_tools import _MCP_TOOLS_LIST
    by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    desc = by_name["resolve_sprint_item_pointers"]["description"]
    assert "freshness_state" in desc
    assert "directory" in desc and "git" in desc and "artifact" in desc
