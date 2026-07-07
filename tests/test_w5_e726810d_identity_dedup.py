"""e726810d — identity alias dedup for active-sessions / standup aggregation.

BUG: "ajc123private" (email local-part from one auth path) and "adam" (display
name from another) surfaced as TWO people in the dashboard active-sessions /
standup view, though both are Adam. The dashboard's ``get_team_summary``
aggregation buckets sessions by their raw ``human_id``, so two handles for one
human split into two people. bdc251ec's ``_resolve_caller_identity`` derived the
handle from whichever tenant field was populated and never reconciled the two.

FIX (meridian/mcp/handler.py): ``_resolve_caller_identity`` now runs the derived
handle through a data-driven alias map (``_canonicalize_identity`` /
``_load_identity_alias_map``) so a known alias set collapses to ONE canonical
display identity before it becomes a ``human_id``. The map is seeded with the one
known alias set and is overridable via the ``MERIDIAN_IDENTITY_ALIASES`` env var
(JSON) — no schema column.

These tests are pure/unit-level: they exercise the resolver + alias helpers
directly and simulate the ``get_team_summary`` bucket-by-identity aggregation
in-process. NO servers, ports, network, DB, or sleeps.
"""
from __future__ import annotations

import json

import pytest

import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
from meridian.mcp import handler as mh


# ---------------------------------------------------------------------------
# Core bug: the two aliases collapse to ONE canonical identity.
# ---------------------------------------------------------------------------

def test_two_aliases_resolve_to_one_canonical_identity():
    # Auth path A: only the email is populated -> local-part "ajc123private".
    ident_email = mh._resolve_caller_identity({"email": "ajc123private@gmail.com"})
    # Auth path B: the display name "adam" is populated.
    ident_name = mh._resolve_caller_identity({"name": "adam"})
    assert ident_email == ident_name == "Adam"


def test_canonicalize_identity_is_case_insensitive_and_trims():
    for handle in ("ajc123private", "AJC123PRIVATE", "  Adam  ", "ADAM", "adam"):
        assert mh._canonicalize_identity(handle) == "Adam"


def test_canonicalize_identity_passes_unknown_handles_through():
    # A handle we were never told about is returned unchanged (never invented/merged).
    assert mh._canonicalize_identity("someone-else") == "someone-else"
    assert mh._canonicalize_identity("jdoe") == "jdoe"


def test_canonicalize_identity_none_and_empty():
    assert mh._canonicalize_identity(None) is None
    assert mh._canonicalize_identity("") is None
    assert mh._canonicalize_identity("   ") is None


# ---------------------------------------------------------------------------
# The bdc251ec base contract must still hold (near-miss handles pass through).
# ---------------------------------------------------------------------------

def test_bdc251ec_base_contract_preserved():
    assert mh._resolve_caller_identity(None) is None
    assert mh._resolve_caller_identity({}) is None
    # ajc123 (NOT ajc123private) is a distinct handle -> unchanged.
    assert mh._resolve_caller_identity({"email": "ajc123@gmail.com"}) == "ajc123"
    # A full display name that isn't a seeded alias passes through unchanged.
    assert mh._resolve_caller_identity(
        {"name": "Adam Camerer", "email": "ajc@x.com"}
    ) == "Adam Camerer"
    assert mh._resolve_caller_identity({"email": "weird"}) == "weird"
    assert mh._resolve_caller_identity({"email": ""}) is None
    # Explicit name still wins over email (and is canonicalized).
    assert mh._resolve_caller_identity(
        {"name": "adam", "email": "different@x.com"}
    ) == "Adam"


# ---------------------------------------------------------------------------
# End goal: two sessions under the two aliases aggregate to ONE person.
# Mirrors get_team_summary's "bucket by human_id" reduction, but keyed on the
# canonical identity the resolver now produces.
# ---------------------------------------------------------------------------

def _aggregate_by_human(session_tenants):
    """In-process stand-in for get_team_summary's group-by-human_id reduction.

    Resolves each session's caller identity the same way the MCP handler does,
    then buckets by that identity — exactly the aggregation that split Adam in
    two before the fix.
    """
    humans: dict[str, dict] = {}
    for tenant in session_tenants:
        hid = mh._resolve_caller_identity(tenant)
        if not hid:
            continue
        humans.setdefault(hid, {"human_id": hid, "sessions": 0})["sessions"] += 1
    return humans


def test_two_alias_sessions_aggregate_to_one_person():
    sessions = [
        {"email": "ajc123private@gmail.com"},  # session 1 registered via email path
        {"name": "adam"},                      # session 2 registered via display name
    ]
    humans = _aggregate_by_human(sessions)
    assert list(humans.keys()) == ["Adam"]
    assert humans["Adam"]["sessions"] == 2


def test_distinct_people_are_not_merged():
    sessions = [
        {"email": "ajc123private@gmail.com"},  # Adam
        {"name": "adam"},                      # Adam
        {"email": "jdoe@example.com"},         # a different, unaliased person
    ]
    humans = _aggregate_by_human(sessions)
    assert set(humans.keys()) == {"Adam", "jdoe"}
    assert humans["Adam"]["sessions"] == 2
    assert humans["jdoe"]["sessions"] == 1


# ---------------------------------------------------------------------------
# Data-driven & overridable: MERIDIAN_IDENTITY_ALIASES extends the seed.
# ---------------------------------------------------------------------------

def test_default_alias_map_contains_seed():
    amap = mh._load_identity_alias_map()
    assert amap["ajc123private"] == "Adam"
    assert amap["adam"] == "Adam"


def test_env_override_extends_alias_map(monkeypatch):
    monkeypatch.setenv(
        "MERIDIAN_IDENTITY_ALIASES",
        json.dumps({"bugsy": "Bianca", "b.wong": "Bianca"}),
    )
    # New person's two handles now collapse...
    assert mh._resolve_caller_identity({"name": "bugsy"}) == "Bianca"
    assert mh._resolve_caller_identity({"email": "b.wong@corp.io"}) == "Bianca"
    # ...and the seeded default is preserved (override is additive, not destructive).
    assert mh._resolve_caller_identity({"email": "ajc123private@x.com"}) == "Adam"


def test_env_override_can_repoint_existing_alias(monkeypatch):
    monkeypatch.setenv(
        "MERIDIAN_IDENTITY_ALIASES", json.dumps({"adam": "Adam Camerer"})
    )
    assert mh._resolve_caller_identity({"name": "adam"}) == "Adam Camerer"


def test_malformed_env_override_is_ignored(monkeypatch):
    # Bad JSON must never break identity resolution — fall back to the seed.
    monkeypatch.setenv("MERIDIAN_IDENTITY_ALIASES", "{not valid json")
    assert mh._resolve_caller_identity({"name": "adam"}) == "Adam"
    # A non-object JSON value is also ignored.
    monkeypatch.setenv("MERIDIAN_IDENTITY_ALIASES", json.dumps(["adam", "x"]))
    assert mh._resolve_caller_identity({"name": "adam"}) == "Adam"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
