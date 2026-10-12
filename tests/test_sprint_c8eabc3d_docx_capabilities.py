"""Regression tests for the shared DOCX capability manifest (sprint c8eabc3d).

Pins three things:
  * the manifest is deterministic and its hash detects any contract change;
  * the manifest agrees exactly with the tunnel-layer region-claim guard
    (meridian/routes/tunnel.py _DOCS_WRITE_TOOLS / _docs_write_target);
  * the known ungated writers are reported as ungated, so closing that gap
    forces a manifest update in the same change.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from meridian import docx_capabilities as dc
from meridian.routes import tunnel as tun

# The meridian-docs MCP server is the source of truth for which DOCX writers
# exist and which of them name the tunnel-layer guard. The parity checks below
# derive their expected sets from it, so a guard-mentioning writer that the
# manifest omits fails this file instead of reading as nonexistent.
DOCS_SERVER_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "extensions"
    / "meridian-docs"
    / "meridian_docs"
    / "server.py"
)
GUARD_PHRASE = "region-claim guard"

# DOCX writers whose docstrings name the guard but that the manifest previously
# omitted, so they were reported as nonexistent rather than as ungated.
TABLE_WRITERS_PREVIOUSLY_MISSING = (
    "insert_table",
    "insert_column",
    "remove_table",
    "split_cell",
    "transpose_table",
)
SIDECAR_WRITERS = {"index_document", "index_document_structure", "index_equations"}


def _entries(manifest: dict) -> dict[str, dict]:
    return {entry["operation"]: entry for entry in manifest["operations"]}


def _is_mcp_tool(node: ast.AST) -> bool:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr == "tool":
            return True
    return False


def _source_guard_writers() -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return every meridian-docs MCP tool whose docstring names the guard.

    Parsed statically (no import of the extension), keyed by function name.
    Whitespace in the docstring is normalised so a phrase wrapped across lines
    still matches.
    """
    source = DOCS_SERVER_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(DOCS_SERVER_SOURCE))
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_mcp_tool(node):
            continue
        docstring = " ".join((ast.get_docstring(node) or "").split())
        if GUARD_PHRASE in docstring:
            found[node.name] = node
    return found


def _guard_writer_names() -> set[str]:
    return set(_source_guard_writers())


# ---------------------------------------------------------------------------
# Determinism and hashing
# ---------------------------------------------------------------------------


def test_manifest_is_deterministic_and_hash_matches_canonical_body():
    first = dc.build_docx_capability_manifest()
    second = dc.build_docx_capability_manifest()
    assert first == second

    body = {k: v for k, v in first.items() if k != "manifest_hash"}
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert first["manifest_hash"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    # Must survive a plain JSON round trip (no non-serialisable values).
    assert json.loads(json.dumps(first, sort_keys=True)) == first


def test_manifest_is_sorted_unique_and_counted():
    manifest = dc.build_docx_capability_manifest()
    names = [entry["operation"] for entry in manifest["operations"]]
    assert names == sorted(names)
    assert len(names) == len(set(names))
    assert manifest["count"] == len(names) == len(dc.DOCX_OPERATIONS)
    assert manifest["schema"] == dc.DOCX_CAPABILITY_SCHEMA


def test_manifest_hash_changes_when_contract_changes():
    base = dc.build_docx_capability_manifest()
    flipped = [
        dataclasses.replace(op, gated_by_tunnel_guard=not op.gated_by_tunnel_guard)
        if op.operation == "insert_image"
        else op
        for op in dc.DOCX_OPERATIONS
    ]
    changed = dc.build_docx_capability_manifest(flipped)
    assert changed["manifest_hash"] != base["manifest_hash"]


# ---------------------------------------------------------------------------
# Parity with the tunnel-layer guard
# ---------------------------------------------------------------------------


def test_gated_operations_match_tunnel_write_table_exactly():
    manifest = dc.build_docx_capability_manifest()
    gated = {
        entry["operation"]: (entry["path_arg"], entry["anchor_arg"])
        for entry in manifest["operations"]
        if entry["gated_by_tunnel_guard"]
    }
    assert gated == dict(tun._DOCS_WRITE_TOOLS)


def test_source_scan_finds_the_guard_writers_it_is_meant_to_check():
    # Guards the scan itself: an empty or broken scan would make the parity
    # checks below pass vacuously.
    names = _guard_writer_names()
    assert names, "no guard-mentioning MCP tools found in meridian-docs server.py"
    assert DOCS_SERVER_SOURCE.is_file()
    assert set(TABLE_WRITERS_PREVIOUSLY_MISSING) <= names


@pytest.mark.parametrize("name", TABLE_WRITERS_PREVIOUSLY_MISSING)
def test_table_writers_are_declared_as_ungated_docx_writers(name):
    entries = _entries(dc.build_docx_capability_manifest())
    assert name in entries, f"{name} names the tunnel guard but is missing from the manifest"
    entry = entries[name]
    assert entry["rw_class"] == dc.RW_DOCX_WRITE
    assert entry["gated_by_tunnel_guard"] is False
    assert name in dc.UNGATED_DOCX_WRITERS
    assert name not in tun._DOCS_WRITE_TOOLS
    target = dc.extract_docx_write_target(name, {"docx_path": "C:/d/x.docx"})
    assert target is not None
    assert target.gated_by_tunnel_guard is False


def test_ungated_writers_are_exactly_the_guard_writers_missing_from_the_gate():
    expected_ungated = {name for name in _guard_writer_names() if name not in tun._DOCS_WRITE_TOOLS}
    assert expected_ungated, "expected at least one guard-mentioning writer outside the gate"
    manifest = dc.build_docx_capability_manifest()
    ungated = {
        entry["operation"]
        for entry in manifest["operations"]
        if entry["rw_class"] == dc.RW_DOCX_WRITE and not entry["gated_by_tunnel_guard"]
    }
    assert ungated == expected_ungated
    assert dc.UNGATED_DOCX_WRITERS == frozenset(expected_ungated)
    # Tripwire: none of them may appear in the live gate table without the
    # manifest flag being flipped in the same change.
    assert expected_ungated.isdisjoint(tun._DOCS_WRITE_TOOLS)


def test_every_guard_mentioning_docs_writer_is_declared_with_matching_gate_status():
    entries = _entries(dc.build_docx_capability_manifest())
    for name in sorted(_guard_writer_names()):
        assert name in entries, f"{name} names the tunnel guard but is missing from the manifest"
        assert entries[name]["gated_by_tunnel_guard"] is (name in tun._DOCS_WRITE_TOOLS), name


def test_declared_path_and_anchor_args_exist_in_source_signatures():
    entries = _entries(dc.build_docx_capability_manifest())
    for name, node in _source_guard_writers().items():
        assert name in entries, name
        params = {arg.arg for arg in [*node.args.args, *node.args.kwonlyargs]}
        entry = entries[name]
        assert entry["path_arg"] in params, name
        if entry["anchor_arg"] is not None:
            assert entry["anchor_arg"] in params, name


def test_sidecar_writers_are_not_gated_and_take_no_lock():
    entries = _entries(dc.build_docx_capability_manifest())
    assert set(entries) >= SIDECAR_WRITERS
    for name in SIDECAR_WRITERS:
        entry = entries[name]
        assert entry["rw_class"] == dc.RW_SIDECAR_WRITE
        assert entry["gated_by_tunnel_guard"] is False
        assert entry["lock_scope"] == dc.LOCK_NONE
        assert entry["anchor_model"] == dc.ANCHOR_NONE
        assert entry["fallback_policy"] == dc.FALLBACK_NOT_APPLICABLE
        assert entry["sidecar_effects"] == [dc.SIDECAR_SQLITE_INDEX]
        assert name not in tun._DOCS_WRITE_TOOLS


def test_anchor_model_lock_and_fallback_are_consistent():
    entries = _entries(dc.build_docx_capability_manifest())
    for name, entry in entries.items():
        if entry["rw_class"] != dc.RW_DOCX_WRITE:
            continue
        if entry["anchor_arg"] is None:
            assert entry["anchor_model"] == dc.ANCHOR_WHOLE_DOCUMENT, name
            assert entry["lock_scope"] == dc.LOCK_WHOLE_DOCUMENT, name
            assert entry["fallback_policy"] == dc.FALLBACK_WHOLE_DOCUMENT, name
        else:
            assert entry["anchor_model"] == dc.ANCHOR_ELEMENT, name
            assert entry["lock_scope"] == dc.LOCK_SCOPED_ELEMENT, name
            assert entry["fallback_policy"] == dc.FALLBACK_WHOLE_DOCUMENT_WHEN_ANCHOR_ABSENT, name

    assert entries["merge_docx_draft"]["path_arg"] == "canonical_path"
    assert entries["merge_docx_draft"]["lock_scope"] == dc.LOCK_WHOLE_DOCUMENT
    assert entries["insert_image"]["anchor_arg"] == "anchor_para_id"
    assert entries["relocate_figure"]["lock_scope"] == dc.LOCK_WHOLE_DOCUMENT


# ---------------------------------------------------------------------------
# Name resolution and aliases
# ---------------------------------------------------------------------------


def test_resolve_accepts_bare_and_prefixed_names():
    assert dc.resolve_operation("insert_image").operation == "insert_image"
    assert dc.resolve_operation("meridian-docs__insert_image").operation == "insert_image"
    assert dc.resolve_operation("index_equations").rw_class == dc.RW_SIDECAR_WRITE
    assert dc.resolve_operation("get_structure") is None
    assert dc.resolve_operation("no_such_tool") is None
    assert dc.resolve_operation("") is None


def test_aliases_resolve_to_canonical_operation_and_appear_in_manifest():
    custom = (
        dc.DocxOperation(
            "write_thing",
            dc.RW_DOCX_WRITE,
            "docx_path",
            aliases=("write_alias",),
            gated_by_tunnel_guard=True,
        ),
    )
    assert dc.resolve_operation("write_alias", custom).operation == "write_thing"
    assert dc.resolve_operation("meridian-docs__write_alias", custom).operation == "write_thing"
    manifest = dc.build_docx_capability_manifest(custom)
    assert manifest["operations"][0]["aliases"] == ["write_alias"]


def test_alias_collision_with_another_operation_is_rejected():
    clash = (
        dc.DocxOperation("alpha", dc.RW_DOCX_WRITE, "docx_path", aliases=("beta",)),
        dc.DocxOperation("beta", dc.RW_DOCX_WRITE, "docx_path"),
    )
    with pytest.raises(ValueError, match="duplicate"):
        dc.build_docx_capability_manifest(clash)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"operation": "", "rw_class": dc.RW_DOCX_WRITE, "path_arg": "docx_path"},
        {"operation": "a__b", "rw_class": dc.RW_DOCX_WRITE, "path_arg": "docx_path"},
        {"operation": "x", "rw_class": "bogus", "path_arg": "docx_path"},
        {"operation": "x", "rw_class": dc.RW_DOCX_WRITE, "path_arg": ""},
        {
            "operation": "x",
            "rw_class": dc.RW_DOCX_WRITE,
            "path_arg": "docx_path",
            "anchor_arg": "docx_path",
        },
        {
            "operation": "x",
            "rw_class": dc.RW_DOCX_WRITE,
            "path_arg": "docx_path",
            "sidecar_effects": ("sqlite_index_cache",),
        },
        {
            "operation": "x",
            "rw_class": dc.RW_SIDECAR_WRITE,
            "path_arg": "path",
            "sidecar_effects": ("sqlite_index_cache",),
            "gated_by_tunnel_guard": True,
        },
        {"operation": "x", "rw_class": dc.RW_SIDECAR_WRITE, "path_arg": "path"},
        {"operation": "x", "rw_class": dc.RW_DOCX_WRITE, "path_arg": "docx_path", "aliases": ("x",)},
        {"operation": "x", "rw_class": dc.RW_DOCX_WRITE, "path_arg": "docx_path", "aliases": ("a", "a")},
    ],
)
def test_invalid_operation_specs_are_rejected(kwargs):
    with pytest.raises(ValueError):
        dc.DocxOperation(**kwargs)


# ---------------------------------------------------------------------------
# Adapter behaviour matches the tunnel resolver
# ---------------------------------------------------------------------------


def _argument_shapes(path_key: str, anchor_key: str | None) -> list:
    shapes: list = [
        None,
        {},
        {path_key: "  C:/docs/paper.docx  "},
        {path_key: ""},
        {path_key: "   "},
        {path_key: 7},
    ]
    if anchor_key is not None:
        shapes += [
            {path_key: "C:/docs/paper.docx", anchor_key: "  p1a2b3c4  "},
            {path_key: "C:/docs/paper.docx", anchor_key: "   "},
            {path_key: "C:/docs/paper.docx", anchor_key: 5},
            {path_key: "C:/docs/paper.docx"},
        ]
    return shapes


@pytest.mark.parametrize("name", sorted(tun._DOCS_WRITE_TOOLS))
def test_adapter_agrees_with_tunnel_resolver_for_gated_tools(name):
    path_key, anchor_key = tun._DOCS_WRITE_TOOLS[name]
    for args in _argument_shapes(path_key, anchor_key):
        expected = tun._docs_write_target(name, args)
        target = dc.extract_docx_write_target(name, args)
        if expected is None:
            assert target is None, (name, args)
        else:
            assert target is not None, (name, args)
            assert (target.docx_path, target.element_id) == expected, (name, args)
            assert target.gated_by_tunnel_guard is True
        # The connector-prefixed spelling must resolve identically.
        prefixed = tun._docs_write_target(f"meridian-docs__{name}", args)
        assert prefixed == expected
        assert (dc.extract_docx_write_target(f"meridian-docs__{name}", args) is None) == (
            target is None
        )


def test_adapter_reports_scope_and_fallback_for_anchored_writes():
    scoped = dc.extract_docx_write_target(
        "insert_image", {"docx_path": "C:/d/x.docx", "anchor_para_id": "p9"}
    )
    assert scoped.element_id == "p9"
    assert scoped.lock_scope == dc.LOCK_SCOPED_ELEMENT
    assert scoped.fallback_applied is False

    fallback = dc.extract_docx_write_target("insert_image", {"docx_path": "C:/d/x.docx"})
    assert fallback.element_id is None
    assert fallback.lock_scope == dc.LOCK_WHOLE_DOCUMENT
    assert fallback.fallback_applied is True


def test_adapter_whole_document_writes_never_report_fallback():
    target = dc.extract_docx_write_target("relocate_figure", {"docx_path": "C:/d/x.docx"})
    assert target.element_id is None
    assert target.lock_scope == dc.LOCK_WHOLE_DOCUMENT
    assert target.fallback_applied is False


def test_adapter_reports_known_ungated_writers_as_ungated():
    target = dc.extract_docx_write_target(
        "remove_cross_reference", {"docx_path": "C:/d/x.docx", "anchor_para_id": "p3"}
    )
    assert target is not None
    assert target.gated_by_tunnel_guard is False
    # The live tunnel gate still does not see it: this is the reported gap.
    assert tun._docs_write_target(
        "remove_cross_reference", {"docx_path": "C:/d/x.docx", "anchor_para_id": "p3"}
    ) is None


@pytest.mark.parametrize(
    "name, args",
    [
        ("get_structure", {"index_db_path": "C:/d/i.db"}),
        ("index_equations", {"path": "C:/d/x.docx", "index_db_path": "C:/d/i.db"}),
        ("no_such_tool", {"docx_path": "C:/d/x.docx"}),
    ],
)
def test_adapter_returns_none_for_reads_sidecars_and_unknown_tools(name, args):
    assert dc.extract_docx_write_target(name, args) is None
    assert tun._docs_write_target(name, args) is None
