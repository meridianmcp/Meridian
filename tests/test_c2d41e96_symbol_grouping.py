"""Focused acceptance tests for c2d41e96 — canonical symbol-resource parsing
and same-file disjoint-symbol conflict grouping.

c2d41e96's own scope (verbatim from the sprint-item notes): "Implement
canonical symbol resource parsing and same-file disjoint-symbol conflict
grouping. Use symbol:path::symbol as the canonical form; whole-file locks
must conflict with all symbols in that file, while distinct symbols may
co-schedule. Preserve legacy compatibility only through an explicit
normalization path. Add focused grouping tests."

Prospecting for this item found the contract already fully implemented and
extensively covered by tests/test_resource_locks.py (63b030a6, 2a176d6d,
6b3b2c0e, de730a25, 501ec93f) — no production code change was made here (see
the c2d41e96 sprint-item report for the full audit trail). This module is the
dedicated, self-contained regression suite the item's own notes ask for,
anchored to the exact contract wording above rather than duplicating the
broader resource-lock suite. It exercises:

  * ``meridian.db.normalize_resource_id`` — the canonical
    ``symbol:<path>::<symbol>`` form (path-only normalization, symbol scope
    preserved byte-for-byte).
  * ``meridian.db._resource_file_of`` / ``_is_legacy_file_symbol_shorthand`` /
    ``_two_resources_conflict`` / ``_resource_sets_conflict`` — the explicit
    legacy-compatibility normalization path for the single-colon
    ``file:<path>:<symbol>`` shorthand, kept OUT of the canonical/stored form.
  * ``meridian.db._predict_resource_granularity`` — static classification of
    a malformed bare ``symbol:<name>`` (no ``::`` file scope).
  * ``meridian.db.sprint_items.get_parallelizable_groups`` (re-exported as
    ``meridian.db.get_parallelizable_groups``) — the same-file conflict
    grouping itself: a whole-file lock conflicts with every symbol in that
    file; distinct symbols in the same file co-schedule; different files
    never conflict even when a symbol name is reused.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module


# ---------------------------------------------------------------------------
# Canonical form: symbol:<path>::<symbol>
# ---------------------------------------------------------------------------


def test_canonical_symbol_form_normalizes_path_component_only():
    """Path separators/leading './' normalize; the '::<symbol>' scope and the
    symbol name itself are preserved byte-for-byte."""
    canonical = db_module.normalize_resource_id(
        "symbol:meridian/db/sprint_items.py::get_parallelizable_groups"
    )
    assert canonical == "symbol:meridian/db/sprint_items.py::get_parallelizable_groups"
    # Backslashes + a leading "./" collapse onto the SAME canonical id as the
    # forward-slash form above — canonical form is about real file identity,
    # not the literal spelling used at declaration time.
    assert db_module.normalize_resource_id(
        "symbol:.\\meridian\\db\\sprint_items.py::get_parallelizable_groups"
    ) == canonical
    assert "symbol" in db_module.RESOURCE_TYPES


def test_malformed_bare_symbol_has_no_file_scope_and_is_classified():
    """A bare 'symbol:<name>' (no '::' file scope) is NOT the canonical form.
    It still best-effort round-trips through parse_touches_resources (never
    silently dropped), but is statically classified 'malformed_symbol' —
    distinct from a well-formed 'symbol:<path>::<name>' resource — so a
    caller can see the declaration is not same-file-conflict-safe."""
    predict = db_module._predict_resource_granularity
    assert predict("symbol:helper_fn") == "malformed_symbol"
    assert predict("symbol:mod.py::helper_fn") == "symbol"
    assert db_module.parse_touches_resources('["symbol:helper_fn"]') == ["symbol:helper_fn"]


# ---------------------------------------------------------------------------
# Legacy compatibility: preserved ONLY via the explicit normalization path
# (_resource_file_of / _is_legacy_file_symbol_shorthand / _two_resources_conflict),
# never by silently rewriting the stored/canonical value.
# ---------------------------------------------------------------------------


def test_legacy_file_symbol_shorthand_untouched_in_storage():
    """The legacy single-colon 'file:<path>:<symbol>' shorthand is not the
    canonical 'symbol:<path>::<symbol>' form, and normalize_resource_id
    deliberately leaves it byte-for-byte untouched in storage — legacy
    compatibility never mutates what gets persisted."""
    assert db_module.normalize_resource_id("file:mod.py:helper") == "file:mod.py:helper"
    assert db_module._is_legacy_file_symbol_shorthand("file:mod.py:helper") is True
    assert db_module._is_legacy_file_symbol_shorthand("file:mod.py") is False
    assert db_module._is_legacy_file_symbol_shorthand("symbol:mod.py::helper") is False


def test_legacy_file_symbol_shorthand_resolves_via_explicit_path_for_conflicts():
    """Legacy compatibility is achieved ONLY through the explicit, named
    _resource_file_of/_two_resources_conflict normalization path: the
    shorthand resolves to the same real file identity as a genuine
    'file:<path>' declaration, and — new coverage — as a canonical
    'symbol:<path>::<name>' declaration on that same real file too."""
    assert db_module._resource_file_of("file:mod.py:helper") == "mod.py"
    assert db_module._two_resources_conflict("file:mod.py:helper", "file:mod.py") is True
    assert db_module._two_resources_conflict(
        "file:mod.py:helper", "symbol:mod.py::other_fn"
    ) is True
    # A different real file is unaffected even with the identical suffix shape.
    assert db_module._two_resources_conflict(
        "file:mod.py:helper", "file:other.py:helper"
    ) is False


# ---------------------------------------------------------------------------
# Same-file disjoint-symbol conflict grouping — get_parallelizable_groups.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whole_file_lock_conflicts_with_every_symbol_in_that_file(db):
    """A whole-file lock must conflict with ALL symbols declared in that
    file — never co-schedule with any of them — while the symbols, being
    pairwise distinct, still co-schedule with EACH OTHER. Order-independent:
    asserted by group membership, not group index."""
    p = await db_module.create_project(db, "c2d41e96-file-vs-all-symbols")
    pid = p["id"]
    await db_module.add_sprint_item(
        db, pid, "v1", "whole file", touches_resources=["file:mod.py"], force=True,
    )
    for name in ("a", "b", "c"):
        await db_module.add_sprint_item(
            db, pid, "v1", f"symbol {name}",
            touches_resources=[f"symbol:mod.py::{name}"], force=True,
        )
    res = await db_module.get_parallelizable_groups(db, pid, version="v1")
    assert res["eligible_count"] == 4
    assert res["group_count"] == 2
    group_titles = [{it["title"] for it in g} for g in res["groups"]]
    file_group = next(g for g in group_titles if "whole file" in g)
    symbol_group = next(g for g in group_titles if g != file_group)
    assert file_group == {"whole file"}
    assert symbol_group == {"symbol a", "symbol b", "symbol c"}


@pytest.mark.asyncio
async def test_distinct_symbols_same_file_all_co_schedule(db):
    """Three distinct symbols declared on the SAME file must all land in one
    parallel-safe group together — same-file disjointness is per-symbol, not
    per-file."""
    p = await db_module.create_project(db, "c2d41e96-symbols-co-schedule")
    pid = p["id"]
    for name in ("f", "g", "h"):
        await db_module.add_sprint_item(
            db, pid, "v1", f"edit {name}",
            touches_resources=[f"symbol:shared.py::{name}"], force=True,
        )
    res = await db_module.get_parallelizable_groups(db, pid, version="v1")
    assert res["group_count"] == 1
    assert len(res["groups"][0]) == 3


@pytest.mark.asyncio
async def test_legacy_shorthand_and_canonical_symbol_same_file_conflict(db):
    """Cross-form coverage: the legacy 'file:<path>:<symbol>' shorthand and a
    canonical 'symbol:<path>::<symbol>' declaration on the SAME real file
    must still be recognized as conflicting — the legacy normalization path
    and the canonical form must agree on real file identity."""
    p = await db_module.create_project(db, "c2d41e96-legacy-vs-canonical")
    pid = p["id"]
    await db_module.add_sprint_item(
        db, pid, "v1", "legacy shorthand",
        touches_resources=["file:mod.py:helper"], force=True,
    )
    await db_module.add_sprint_item(
        db, pid, "v1", "canonical symbol",
        touches_resources=["symbol:mod.py::other_fn"], force=True,
    )
    res = await db_module.get_parallelizable_groups(db, pid, version="v1")
    assert res["group_count"] == 2


@pytest.mark.asyncio
async def test_same_symbol_name_on_different_files_never_conflicts(db):
    """Canonical parsing keys conflict on the FILE PATH, not the bare symbol
    name — two different real files declaring the identically-named symbol
    must co-schedule, never falsely conflict."""
    p = await db_module.create_project(db, "c2d41e96-different-files-same-name")
    pid = p["id"]
    await db_module.add_sprint_item(
        db, pid, "v1", "file a symbol",
        touches_resources=["symbol:file_a.py::run"], force=True,
    )
    await db_module.add_sprint_item(
        db, pid, "v1", "file b symbol",
        touches_resources=["symbol:file_b.py::run"], force=True,
    )
    res = await db_module.get_parallelizable_groups(db, pid, version="v1")
    assert res["group_count"] == 1
    assert len(res["groups"][0]) == 2
