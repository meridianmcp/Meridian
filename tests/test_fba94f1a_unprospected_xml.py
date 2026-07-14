"""fba94f1a — tests for the unprospected="true" signal in build_sprint_items_xml.

These tests verify that build_sprint_items_xml emits the unprospected="true"
attribute on items that have no real prospecting evidence, and omits it on
items that do have evidence.
"""
import sys
import os

# Make meridian importable from the worktree root.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import meridian.db as db_module
from meridian.db.sprint_items import _item_is_unprospected, build_sprint_items_xml


# ---------------------------------------------------------------------------
# Unit tests for _item_is_unprospected
# ---------------------------------------------------------------------------

def _item(**kwargs):
    """Build a minimal sprint item dict with the given fields."""
    base = {"id": "abc", "version": "v1", "status": "pending", "title": "T"}
    base.update(kwargs)
    return base


def test_unprospected_plain_db_row():
    """A plain DB row (no prospect_status, no pointers) is unprospected."""
    assert _item_is_unprospected(_item()) is True


def test_unprospected_no_match():
    """prospect_status='no_match' means enrichment ran but found nothing — unprospected."""
    assert _item_is_unprospected(_item(prospect_status="no_match")) is True


def test_unprospected_no_valid_match():
    """prospect_status='no_valid_match' — enrichment ran but pointer was invalid."""
    assert _item_is_unprospected(_item(prospect_status="no_valid_match")) is True


def test_unprospected_no_query():
    """prospect_status='no_query' — no extractable keywords, still unprospected."""
    assert _item_is_unprospected(_item(prospect_status="no_query")) is True


def test_unprospected_error():
    """prospect_status='error' — enrichment errored, still unprospected."""
    assert _item_is_unprospected(_item(prospect_status="error")) is True


def test_prospected_status():
    """prospect_status='prospected' — a real match was found, NOT unprospected."""
    assert _item_is_unprospected(_item(prospect_status="prospected")) is False


def test_cached_status():
    """prospect_status='cached' — prior pointer reused, NOT unprospected."""
    assert _item_is_unprospected(_item(prospect_status="cached")) is False


def test_skipped_manual_not_flagged():
    """prospect_status='skipped_manual' (human/MANUAL item) — intentional skip,
    NOT flagged as unprospected."""
    assert _item_is_unprospected(_item(prospect_status="skipped_manual")) is False


def test_skipped_cap_not_flagged():
    """prospect_status='skipped_cap' (beyond enrichment cap) — intentional,
    NOT flagged as unprospected."""
    assert _item_is_unprospected(_item(prospect_status="skipped_cap")) is False


def test_no_backend_not_flagged():
    """prospect_status='no_backend' — no searcher wired for this source type,
    intentional skip, NOT flagged as unprospected."""
    assert _item_is_unprospected(_item(prospect_status="no_backend")) is False


def test_code_pointers_present():
    """Non-empty code_pointers means real evidence exists — NOT unprospected."""
    assert _item_is_unprospected(_item(code_pointers=[{"file": "foo.py"}])) is False


def test_pointers_present():
    """Non-empty pointers means real evidence exists — NOT unprospected."""
    assert _item_is_unprospected(_item(pointers=[{"uri": "doc://x"}])) is False


def test_empty_code_pointers_still_unprospected():
    """An empty list for code_pointers is falsy — still unprospected."""
    assert _item_is_unprospected(_item(code_pointers=[])) is True


def test_empty_pointers_still_unprospected():
    """An empty list for pointers is falsy — still unprospected."""
    assert _item_is_unprospected(_item(pointers=[])) is True


def test_none_code_pointers_still_unprospected():
    """Explicit None for code_pointers — still unprospected."""
    assert _item_is_unprospected(_item(code_pointers=None)) is True


# ---------------------------------------------------------------------------
# Integration tests for build_sprint_items_xml unprospected attribute
# ---------------------------------------------------------------------------

def _make_item(iid, title, *, prospect_status=None, code_pointers=None, pointers=None,
               group=None, pushed_to=None, status="pending", version="v1"):
    d = {
        "id": iid,
        "version": version,
        "status": status,
        "title": title,
        "item_group": group,
        "pushed_to": pushed_to,
    }
    if prospect_status is not None:
        d["prospect_status"] = prospect_status
    if code_pointers is not None:
        d["code_pointers"] = code_pointers
    if pointers is not None:
        d["pointers"] = pointers
    return d


def test_xml_plain_item_has_unprospected_attr():
    """A plain DB item (no enrichment fields) gets unprospected='true' in XML."""
    xml = build_sprint_items_xml([
        _make_item("id-1", "Add auth")
    ])
    assert 'unprospected="true"' in xml


def test_xml_prospected_item_no_unprospected_attr():
    """An item with prospect_status='prospected' does NOT get unprospected attr."""
    xml = build_sprint_items_xml([
        _make_item("id-2", "Fix login", prospect_status="prospected",
                   code_pointers=[{"file": "auth.py"}])
    ])
    assert 'unprospected="true"' not in xml


def test_xml_cached_item_no_unprospected_attr():
    """An item with prospect_status='cached' does NOT get unprospected attr."""
    xml = build_sprint_items_xml([
        _make_item("id-3", "Update routes", prospect_status="cached",
                   code_pointers=[{"file": "routes.py"}])
    ])
    assert 'unprospected="true"' not in xml


def test_xml_mixed_items_selective_attr():
    """Mixed list: only unprospected items carry the attribute."""
    xml = build_sprint_items_xml([
        _make_item("id-1", "Plain item"),
        _make_item("id-2", "Prospected item", prospect_status="prospected",
                   code_pointers=[{"file": "db.py"}]),
        _make_item("id-3", "No match item", prospect_status="no_match"),
    ])
    # id-1 and id-3 are unprospected; id-2 is not.
    lines = xml.splitlines()
    id1_line = next(l for l in lines if 'id="id-1"' in l)
    id2_line = next(l for l in lines if 'id="id-2"' in l)
    id3_line = next(l for l in lines if 'id="id-3"' in l)
    assert 'unprospected="true"' in id1_line
    assert 'unprospected="true"' not in id2_line
    assert 'unprospected="true"' in id3_line


def test_xml_skipped_manual_no_unprospected_attr():
    """Human/MANUAL items with skipped_manual are NOT flagged unprospected."""
    xml = build_sprint_items_xml([
        _make_item("id-4", "Talk to advisor", prospect_status="skipped_manual")
    ])
    assert 'unprospected="true"' not in xml


def test_xml_skipped_cap_no_unprospected_attr():
    """Items beyond enrichment cap (skipped_cap) are NOT flagged unprospected."""
    xml = build_sprint_items_xml([
        _make_item("id-5", "Cap item", prospect_status="skipped_cap")
    ])
    assert 'unprospected="true"' not in xml


def test_xml_with_code_pointers_no_unprospected_attr():
    """Item carrying code_pointers (even without prospect_status) is NOT flagged."""
    xml = build_sprint_items_xml([
        _make_item("id-6", "Has pointers", code_pointers=[{"file": "server.py"}])
    ])
    assert 'unprospected="true"' not in xml


def test_xml_with_generic_pointers_no_unprospected_attr():
    """Item carrying generic pointers is NOT flagged unprospected."""
    xml = build_sprint_items_xml([
        _make_item("id-7", "Doc item", pointers=[{"uri": "doc://x", "selector": "s"}])
    ])
    assert 'unprospected="true"' not in xml


def test_xml_structure_unchanged():
    """Existing attributes and XML structure are not disturbed by the new field."""
    xml = build_sprint_items_xml([
        _make_item("id-1", "Task <A>", group="Auth", pushed_to=None, version="v2",
                   status="todo")
    ])
    assert '<sprint_items cache="false">' in xml
    assert '<group name="Auth">' in xml
    assert 'id="id-1"' in xml
    assert 'version="v2"' in xml
    assert 'status="todo"' in xml
    assert "Task &lt;A&gt;" in xml  # title is XML-escaped
    assert xml.endswith("</sprint_items>")


def test_xml_empty_list():
    """Empty item list produces valid XML with no items."""
    xml = build_sprint_items_xml([])
    assert '<sprint_items cache="false">' in xml
    assert xml.endswith("</sprint_items>")
    assert "item" not in xml or "<item" not in xml


def test_xml_reexported_from_db_module():
    """build_sprint_items_xml is re-exported from meridian.db so callers are unaffected."""
    assert hasattr(db_module, "build_sprint_items_xml")
    assert db_module.build_sprint_items_xml is build_sprint_items_xml
