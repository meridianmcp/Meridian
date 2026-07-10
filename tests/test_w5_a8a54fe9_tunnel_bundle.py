"""Tests for the known-plugin-tools bundling catalog (item a8a54fe9).

Item a8a54fe9 is the general "bundle the known plugin tools (docx-mcp,
codebase-memory-mcp, Serena, DC, ...) as first-class built-ins" gap. The
tunnel's server exposes a *fixed* set of transport slots, so a plugin can only be
a first-class built-in when it rides a slot that already has a dedicated server
route. This module tests the declarative :data:`KNOWN_PLUGIN_TOOLS` catalog that
makes the bundling state explicit — which known plugin tools are already
built-in vs. which are still owned by a dedicated item (meridian-docs → 9665538a,
zotero → 39c117b1). The catalog documents that remaining gap; it deliberately
does NOT wire those two tools' slots (that is 9665538a / 39c117b1's scope).
"""
from __future__ import annotations

from meridian import tunnel_plugins as tp


# ---------------------------------------------------------------------------
# Catalog shape & accessors
# ---------------------------------------------------------------------------

def test_catalog_accessors_return_fresh_copies():
    # known_plugin_tools() returns shallow copies so a caller can annotate an
    # entry without corrupting the module-level catalog.
    a = tp.known_plugin_tools()
    b = tp.known_plugin_tools()
    assert a == b
    assert a is not b
    a[0]["mutated"] = True
    assert "mutated" not in tp.known_plugin_tools()[0]
    # And the module constant itself is untouched.
    assert "mutated" not in tp.KNOWN_PLUGIN_TOOLS[0]


def test_every_catalog_entry_has_the_required_fields():
    required = {"name", "package", "runtime", "slot", "bundled", "owner_item", "description"}
    for tool in tp.known_plugin_tools():
        assert required <= set(tool), f"{tool.get('name')!r} missing fields"
        assert isinstance(tool["name"], str) and tool["name"]
        assert isinstance(tool["package"], str) and tool["package"]
        assert isinstance(tool["bundled"], bool)
        assert isinstance(tool["description"], str) and tool["description"]


def test_catalog_names_are_unique():
    names = [t["name"] for t in tp.known_plugin_tools()]
    assert len(names) == len(set(names))


def test_runtime_is_a_known_launcher():
    for tool in tp.known_plugin_tools():
        assert tool["runtime"] in {"uvx", "npx", "binary"}, tool


# ---------------------------------------------------------------------------
# Bundled == rides a real built-in slot (the core invariant)
# ---------------------------------------------------------------------------

def test_bundled_tool_iff_it_rides_a_real_builtin_slot():
    # The bundling promise: a tool is `bundled` exactly when it maps to a genuine
    # built-in slot (one of SLOTS) whose name is a built-in plugin. This is what
    # keeps the catalog honest — no tool can claim to be first-class without a
    # server-routed slot behind it.
    builtin = set(tp.builtin_names())
    for tool in tp.known_plugin_tools():
        if tool["bundled"]:
            assert tool["slot"] in tp.SLOTS, tool
            assert tool["name"] in builtin, tool
            assert tool["owner_item"] is None, tool
        else:
            # Not yet first-class → no built-in slot, and an owner item must name
            # who is responsible for wiring it.
            assert tool["slot"] is None, tool
            assert tool["owner_item"], tool


def test_catalog_covers_every_builtin_plugin():
    # Every first-class built-in plugin appears in the catalog as bundled, so the
    # catalog is a complete inventory of what ships today (no built-in is missing
    # from the "what's bundled" answer).
    bundled_names = {t["name"] for t in tp.bundled_plugin_tools()}
    assert bundled_names == set(tp.builtin_names())


def test_bundled_slots_match_the_builtin_plugin_slots():
    # Each bundled catalog entry's slot matches that plugin's actual built-in slot
    # in BUILTIN_PLUGINS — the catalog can't drift from the real registry.
    builtin_slot = {p["name"]: p["slot"] for p in tp.BUILTIN_PLUGINS}
    for tool in tp.bundled_plugin_tools():
        assert tool["slot"] == builtin_slot[tool["name"]], tool


def test_bundled_and_unbundled_partition_the_catalog():
    all_names = {t["name"] for t in tp.known_plugin_tools()}
    bundled = {t["name"] for t in tp.bundled_plugin_tools()}
    unbundled = {t["name"] for t in tp.unbundled_plugin_tools()}
    assert bundled | unbundled == all_names
    assert bundled & unbundled == set()


def test_all_six_known_editors_are_already_bundled():
    # RE-VERIFY the item premise: docx-mcp, codebase-memory-mcp, Serena, DC,
    # powerpoint, filesystem are ALL already bundled built-ins — the item's list
    # of "known plugin tools" is (except meridian-docs / zotero) already first-
    # class. This is the finding that scopes the item down to the catalog.
    bundled = {t["name"]: t for t in tp.bundled_plugin_tools()}
    assert bundled["word"]["package"] == "docx-mcp"
    assert bundled["code-intel"]["package"] == "codebase-memory-mcp"
    assert bundled["code-extractor"]["package"] == "serena-agent"
    assert bundled["desktop-commander"]["package"].endswith("desktop-commander")
    assert bundled["powerpoint"]["package"] == "powerpoint-mcp"
    assert "filesystem" in bundled


# ---------------------------------------------------------------------------
# The remaining gap is precisely the two related items (no duplication)
# ---------------------------------------------------------------------------

def test_unbundled_catalog_is_empty_all_known_tools_bundled():
    # After 9665538a (docs slot) AND 39c117b1 (zotero slot) shipped, every known
    # plugin tool is a first-class bundled built-in — nothing remains catalog-only.
    assert tp.unbundled_plugin_tools() == []
    assert {t["name"] for t in tp.bundled_plugin_tools()} == {
        t["name"] for t in tp.known_plugin_tools()
    }


def test_meridian_docs_is_distinct_from_the_docx_editor():
    # meridian-docs (the parser, on the `docs` slot) is a DIFFERENT tool than
    # docx-mcp (the editor on the `word` slot). Both are bundled now (9665538a
    # shipped docs); confirm the catalog keeps them distinct — different packages
    # and different slots, never conflated.
    by_name = {t["name"]: t for t in tp.known_plugin_tools()}
    assert by_name["meridian-docs"]["package"] == "meridian-docs"
    assert by_name["word"]["package"] == "docx-mcp"
    assert by_name["word"]["bundled"] is True
    assert by_name["meridian-docs"]["bundled"] is True
    assert by_name["meridian-docs"]["slot"] == "docs"
    assert by_name["word"]["slot"] == "word"


def test_owner_items_only_on_unbundled_entries():
    # No bundled (shipped) tool carries an owner_item; every unbundled one does.
    for tool in tp.known_plugin_tools():
        if tool["bundled"]:
            assert tool["owner_item"] is None, tool
        else:
            assert isinstance(tool["owner_item"], str) and tool["owner_item"], tool


# ---------------------------------------------------------------------------
# The catalog does not perturb the existing resolver behaviour
# ---------------------------------------------------------------------------

def test_catalog_addition_does_not_change_resolve_plugins_defaults():
    # Adding the catalog must not alter what the tunnel actually spawns: the
    # resolved built-in set is unchanged (regression guard for the existing
    # 3-slot / 6-slot model).
    resolved = tp.resolve_plugins(None)
    assert [p["name"] for p in resolved] == list(tp.builtin_names())
    # Every catalog tool is now a shipped built-in (docs → 9665538a, zotero →
    # 39c117b1), so the catalog and the runnable built-in set fully agree.
    assert {t["name"] for t in tp.bundled_plugin_tools()} == set(tp.builtin_names())
