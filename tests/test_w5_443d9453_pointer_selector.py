"""443d9453 — add_sprint_item_pointer selector-schema documentation + validation.

The generic-pointer selector schema (2976e168) was underspecified: a ``node_id``
selector needs the field ``id`` (NOT the generic ``value``), and a ``subSelector``
is itself a full selector that must carry its OWN explicit ``type`` (it does not
inherit the parent's). This file locks in that behaviour:

* a ``node_id`` selector with ``id`` VALIDATES and normalizes cleanly;
* a ``node_id`` selector with ``value`` (the wrong key) yields a CLEAR error that
  names the required ``id`` field AND points at the ``value`` typo;
* a ``subSelector`` missing its ``type`` yields a CLEAR error saying each
  subSelector must carry its own explicit ``type``;
* the ``add_sprint_item_pointer`` tool description + input schema now spell the
  precise selector shapes out (``{"type":"node_id","id":...}`` etc.).

Pure/unit level: only ``meridian.pointers`` validation + the static tool metadata
in ``meridian.mcp_tools`` — NO server, port, network, DB, or sleep.
"""
from __future__ import annotations

import pytest

from meridian.pointers import PointerValidationError, validate_pointer
from meridian import mcp_tools


# ---------------------------------------------------------------------------
# node_id selector — 'id' is the right field; 'value' is the wrong one.
# ---------------------------------------------------------------------------

def test_node_id_selector_with_id_resolves():
    """A node_id selector spelled with 'id' validates + normalizes cleanly."""
    ptr = validate_pointer({
        "source_type": "docs",
        "targets": [
            {"uri": "doc:1", "selector": {"type": "node_id", "id": "el-42"}},
        ],
    })
    sel = ptr["targets"][0]["selector"]
    assert sel["type"] == "node_id"
    assert sel["id"] == "el-42"
    # Only the normalized keys survive — no stray fields.
    assert set(sel) == {"type", "id"}


def test_node_id_selector_id_is_stripped():
    ptr = validate_pointer({
        "source_type": "docs",
        "targets": [
            {"uri": "doc:1", "selector": {"type": "node_id", "id": "  el-7  "}},
        ],
    })
    assert ptr["targets"][0]["selector"]["id"] == "el-7"


def test_node_id_selector_with_value_gives_clear_error():
    """'value' instead of 'id' → error that names 'id' AND flags the 'value' typo."""
    with pytest.raises(PointerValidationError) as exc:
        validate_pointer({
            "source_type": "docs",
            "targets": [
                {"uri": "doc:1", "selector": {"type": "node_id", "value": "el-42"}},
            ],
        })
    msg = str(exc.value)
    assert "node_id" in msg
    assert "'id'" in msg  # names the required field
    assert "value" in msg  # points at the wrong key the caller actually used


def test_node_id_selector_missing_id_entirely_gives_clear_error():
    with pytest.raises(PointerValidationError) as exc:
        validate_pointer({
            "source_type": "docs",
            "targets": [{"uri": "doc:1", "selector": {"type": "node_id"}}],
        })
    msg = str(exc.value)
    assert "node_id" in msg and "'id'" in msg


def test_node_id_selector_empty_id_gives_clear_error():
    with pytest.raises(PointerValidationError) as exc:
        validate_pointer({
            "source_type": "docs",
            "targets": [
                {"uri": "doc:1", "selector": {"type": "node_id", "id": "   "}},
            ],
        })
    assert "'id'" in str(exc.value)


# ---------------------------------------------------------------------------
# subSelector — must carry its own explicit 'type'.
# ---------------------------------------------------------------------------

def test_subselector_missing_type_gives_clear_error():
    """A subSelector without its own 'type' → clear, actionable error."""
    with pytest.raises(PointerValidationError) as exc:
        validate_pointer({
            "source_type": "code",
            "targets": [{
                "uri": "a.py",
                "selector": {
                    "type": "symbol", "qualified_name": "a.b.func",
                    # subSelector is missing its own "type":
                    "subSelector": {"start_line": 3, "end_line": 4},
                },
            }],
        })
    msg = str(exc.value)
    assert "subSelector" in msg
    assert "type" in msg
    # The hint must tell the caller each subSelector needs its OWN explicit type.
    assert "own explicit 'type'" in msg


def test_target_level_subselector_missing_type_also_errors():
    """A TARGET-level subSelector (folded into the selector) is validated too."""
    with pytest.raises(PointerValidationError) as exc:
        validate_pointer({
            "source_type": "code",
            "targets": [{
                "uri": "a.py",
                "selector": {"type": "symbol", "qualified_name": "a.b.func"},
                # subSelector as a peer of selector, missing its "type":
                "subSelector": {"start_line": 3, "end_line": 4},
            }],
        })
    msg = str(exc.value)
    assert "subSelector" in msg and "type" in msg


def test_subselector_with_type_validates():
    """A well-formed subSelector (its own 'type') validates + nests correctly."""
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
    sub = ptr["targets"][0]["selector"]["subSelector"]
    assert sub["type"] == "range"
    assert sub["start_line"] == 3 and sub["end_line"] == 4


def test_node_id_subselector_with_value_wrong_key_errors():
    """Wrong-key detection applies recursively to a node_id subSelector too."""
    with pytest.raises(PointerValidationError) as exc:
        validate_pointer({
            "source_type": "docs",
            "targets": [{
                "uri": "doc:1",
                "selector": {
                    "type": "node_id", "id": "el-1",
                    "subSelector": {"type": "node_id", "value": "el-2"},
                },
            }],
        })
    msg = str(exc.value)
    assert "subSelector" in msg and "'id'" in msg


# ---------------------------------------------------------------------------
# Tool schema/description — the selector shapes are now spelled out precisely.
# ---------------------------------------------------------------------------

def _add_pointer_tool() -> dict:
    for tool in mcp_tools._MCP_TOOLS_LIST:
        if tool.get("name") == "add_sprint_item_pointer":
            return tool
    raise AssertionError("add_sprint_item_pointer tool not found in _MCP_TOOLS_LIST")


def test_tool_description_documents_node_id_id_shape():
    desc = _add_pointer_tool()["description"]
    # The precise node_id shape (id, NOT value) is documented.
    assert '"type":"node_id"' in desc
    assert '"id"' in desc
    assert "NOT" in desc and "value" in desc  # the id-not-value caveat


def test_tool_description_documents_subselector_own_type_rule():
    desc = _add_pointer_tool()["description"]
    assert "subSelector" in desc
    # Must state a subSelector carries its OWN explicit type.
    assert "OWN" in desc and "type" in desc


def test_tool_targets_param_documents_node_id_id_field():
    tool = _add_pointer_tool()
    targets_desc = tool["inputSchema"]["properties"]["targets"]["description"]
    assert "node_id" in targets_desc
    assert '"id"' in targets_desc
    assert "value" in targets_desc  # the id-not-value caveat is in the param too


def test_tool_description_spells_all_selector_types():
    desc = _add_pointer_tool()["description"]
    for stype in ("range", "symbol", "node_id", "zotero_key"):
        assert f'"type":"{stype}"' in desc, f"missing explicit shape for {stype}"


# ---------------------------------------------------------------------------
# Sanity: the valid shapes from the docs actually validate (docs == behaviour).
# ---------------------------------------------------------------------------

def test_documented_shapes_all_validate():
    ptr = validate_pointer({
        "source_type": "code",
        "targets": [
            {"uri": "a.py", "selector": {"type": "range", "start_line": 1,
                                         "end_line": 2}},
            {"uri": "a.py", "selector": {"type": "symbol",
                                         "qualified_name": "a.b.f"}},
            {"uri": "doc:1", "selector": {"type": "node_id", "id": "el-1"}},
            {"uri": "zotero:", "selector": {"type": "zotero_key", "key": "ABCD"}},
        ],
    })
    kinds = [t["selector"]["type"] for t in ptr["targets"]]
    assert kinds == ["range", "symbol", "node_id", "zotero_key"]
