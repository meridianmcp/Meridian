"""Unit tests of the member policy core (``meridian/member_policy.py``).

Pure logic: the capability catalog, the effective-policy resolution order, the
scope carrier, the refusal types and the decision functions both transports
share. The database-backed resolver is covered in
``test_member_policy_scope_expansion.py``.
"""
from __future__ import annotations

import copy
import json

import pytest

from meridian import db as db_module
from meridian import member_policy as mp
from meridian.roles import ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER, ROLE_VIEWER

ALL = mp.CAPABILITY_IDS
W, C, DS, DP, WL = (
    mp.WRITE_PROJECT, mp.CREATE_SUBPROJECT, mp.DELETE_SUBPROJECT,
    mp.DELETE_PROJECT, mp.WRITE_WORKSPACE_LEVEL,
)


def _on(policy: mp.EffectivePolicy) -> set[str]:
    return set(policy.caps)


# ---------------------------------------------------------------------------
# catalog and built-in defaults
# ---------------------------------------------------------------------------

def test_catalog_ids_and_builtin_defaults_are_the_owners_policy():
    assert mp.CAPABILITY_IDS == (
        "write_project", "create_subproject", "delete_subproject",
        "delete_project", "write_workspace_level",
    )
    # A scoped member can write their project and its subprojects and create a
    # subproject; they cannot delete either, nor touch workspace-level state.
    assert dict(mp.BUILTIN_DEFAULTS) == {
        "write_project": True, "create_subproject": True, "delete_subproject": False,
        "delete_project": False, "write_workspace_level": False,
    }


def test_catalog_entries_are_complete_and_distinct():
    assert [c.id for c in mp.CAPABILITIES] == list(mp.CAPABILITY_IDS)
    assert len({c.label for c in mp.CAPABILITIES}) == 5
    for cap in mp.CAPABILITIES:
        assert cap.label and cap.sentence and cap.denial and cap.meaning and cap.covers
        assert cap.builtin_default is mp.BUILTIN_DEFAULTS[cap.id]
        assert mp.CAPABILITY_BY_ID[cap.id] is cap
        assert cap.denial.endswith(".")


def test_builtin_tables_are_read_only():
    with pytest.raises(TypeError):
        mp.BUILTIN_DEFAULTS["delete_project"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        mp.CAPABILITY_BY_ID["x"] = None  # type: ignore[index]


def test_project_class_names_match_the_db_layer():
    assert (mp.PROJECT_TOP, mp.PROJECT_SUBPROJECT, mp.PROJECT_ORPHAN) == (
        db_module.PROJECT_CLASS_TOP, db_module.PROJECT_CLASS_SUBPROJECT,
        db_module.PROJECT_CLASS_ORPHAN,
    )


def test_sort_capabilities_uses_catalog_order_and_puts_unknown_last():
    assert mp.sort_capabilities([WL, W, "bogus", DP, C]) == [W, C, DP, WL, "bogus"]


# ---------------------------------------------------------------------------
# effective policy: precedence builtin < default < override, then clamp, then implication
# ---------------------------------------------------------------------------

def test_no_rows_gives_builtin_defaults_with_builtin_sources():
    p = mp.compute_effective_policy(None, None, [ROLE_MEMBER])
    assert p.as_dict() == dict(mp.BUILTIN_DEFAULTS)
    assert set(p.sources.values()) == {mp.SOURCE_BUILTIN}
    assert p.role == ROLE_MEMBER and not p.reasons
    assert p == mp.BUILTIN_POLICY


def test_workspace_default_overlays_builtin_and_names_its_source():
    p = mp.compute_effective_policy('{"delete_subproject": true, "create_subproject": false}',
                                    None, [ROLE_MEMBER])
    assert _on(p) == {W, DS}
    assert p.source(DS) == mp.SOURCE_DEFAULT and p.source(C) == mp.SOURCE_DEFAULT
    assert p.source(W) == mp.SOURCE_BUILTIN and p.source(DP) == mp.SOURCE_BUILTIN


def test_person_override_beats_the_workspace_default_both_ways():
    default = {"delete_subproject": True, "create_subproject": False}
    override = {"delete_subproject": False, "create_subproject": True, "delete_project": True}
    p = mp.compute_effective_policy(default, override, [ROLE_MEMBER])
    assert _on(p) == {W, C, DP}
    assert p.source(DS) == mp.SOURCE_OVERRIDE and p.source(C) == mp.SOURCE_OVERRIDE
    assert p.source(DP) == mp.SOURCE_OVERRIDE and p.source(W) == mp.SOURCE_BUILTIN


def test_override_null_or_absent_inherits_the_default():
    default = {"delete_subproject": True, "delete_project": True}
    p = mp.compute_effective_policy(default, {"delete_subproject": None}, [ROLE_MEMBER])
    assert p.allows(DS) and p.allows(DP)
    assert p.source(DS) == mp.SOURCE_DEFAULT and p.source(DP) == mp.SOURCE_DEFAULT


def test_stored_json_text_and_parsed_dicts_resolve_identically():
    d, o = {"delete_project": True}, {"create_subproject": False}
    assert mp.compute_effective_policy(json.dumps(d), json.dumps(o), [ROLE_MEMBER]) == \
        mp.compute_effective_policy(d, o, [ROLE_MEMBER])
    assert mp.compute_effective_policy(b'{"delete_project": true}', None, [ROLE_MEMBER]).allows(DP)


@pytest.mark.parametrize("raw", [
    "", "{", "not json", "[]", '"x"', "123", "null", "true", '{"write_project": true',
])
def test_a_corrupt_default_row_denies_every_capability(raw):
    p = mp.compute_effective_policy(raw, None, [ROLE_MEMBER])
    assert p.caps == frozenset()
    assert set(p.reasons.values()) == {mp.REASON_CORRUPT_DEFAULT}
    assert set(p.sources.values()) == {mp.SOURCE_DEFAULT}


@pytest.mark.parametrize("raw", ["", "{", "[]", "null", "42"])
def test_a_corrupt_override_row_denies_every_capability_even_over_a_permissive_default(raw):
    permissive = {cid: True for cid in ALL}
    p = mp.compute_effective_policy(permissive, raw, [ROLE_ADMIN])
    assert p.caps == frozenset()
    assert set(p.reasons.values()) == {mp.REASON_CORRUPT_OVERRIDE}
    assert set(p.sources.values()) == {mp.SOURCE_OVERRIDE}


def test_corrupt_row_that_is_not_text_or_a_mapping_is_corrupt():
    assert mp.compute_effective_policy(12, None, [ROLE_MEMBER]).caps == frozenset()
    assert mp.compute_effective_policy(b"\xff\xfe", None, [ROLE_MEMBER]).caps == frozenset()
    assert mp.compute_effective_policy("[" * 100000, None, [ROLE_MEMBER]).caps == frozenset()


def test_unknown_keys_are_ignored_and_never_grant_anything():
    p = mp.compute_effective_policy({"super_admin": True, "write_everything": True},
                                    {"root": True}, [ROLE_MEMBER])
    assert p.as_dict() == dict(mp.BUILTIN_DEFAULTS)
    assert set(p.caps) <= set(ALL)


@pytest.mark.parametrize("bad", ["true", "yes", 1, 0, [], {}, 1.0])
def test_a_non_boolean_value_for_a_known_key_is_false(bad):
    p = mp.compute_effective_policy({"create_subproject": bad}, None, [ROLE_MEMBER])
    assert not p.allows(C) and p.source(C) == mp.SOURCE_DEFAULT
    p = mp.compute_effective_policy(None, {"delete_project": bad}, [ROLE_MEMBER])
    assert not p.allows(DP)
    # an override of a capability that is ON by default turns it off when malformed
    p = mp.compute_effective_policy(None, {"write_project": bad}, [ROLE_MEMBER])
    assert not p.allows(W)


def test_null_in_the_default_row_is_not_inheritance_it_is_false():
    # tri-state null means "inherit" only in an override row; the default row is bools only.
    p = mp.compute_effective_policy({"create_subproject": None}, None, [ROLE_MEMBER])
    assert not p.allows(C)


def test_viewer_anywhere_makes_every_capability_false_and_no_override_helps():
    permissive = {cid: True for cid in ALL}
    for roles in ([ROLE_VIEWER], [ROLE_MEMBER, ROLE_VIEWER], [ROLE_VIEWER, ROLE_ADMIN, ROLE_OWNER]):
        p = mp.compute_effective_policy(permissive, permissive, roles)
        assert p.caps == frozenset() and p.role == ROLE_VIEWER
        assert set(p.sources.values()) == {mp.SOURCE_ROLE_CLAMP}
        assert set(p.reasons.values()) == {mp.REASON_VIEWER_ROLE}


def test_role_is_the_most_restrictive_across_all_accepted_rows():
    assert mp.most_restrictive_role([(ROLE_ADMIN, "p1"), (ROLE_MEMBER, "p2")]) == ROLE_MEMBER
    assert mp.most_restrictive_role([ROLE_OWNER, ROLE_ADMIN]) == ROLE_ADMIN
    assert mp.most_restrictive_role([ROLE_OWNER]) == ROLE_OWNER
    assert mp.most_restrictive_role([]) is None


@pytest.mark.parametrize("weird", ["Admin", "superuser", "", None, 5])
def test_an_unknown_role_counts_as_viewer(weird):
    assert mp.most_restrictive_role([weird]) == ROLE_VIEWER
    assert mp.most_restrictive_role([ROLE_ADMIN, weird]) == ROLE_VIEWER
    assert mp.compute_effective_policy(None, None, [weird]).caps == frozenset()


def test_no_accepted_membership_at_all_denies_everything():
    p = mp.compute_effective_policy({"delete_project": True}, None, [])
    assert p.caps == frozenset() and p.role is None
    assert set(p.reasons.values()) == {mp.REASON_NO_MEMBERSHIP}


def test_admin_and_invited_owner_rows_get_no_bonus_over_member():
    base = mp.compute_effective_policy(None, None, [ROLE_MEMBER])
    for role in (ROLE_ADMIN, ROLE_OWNER):
        p = mp.compute_effective_policy(None, None, [role])
        assert p.caps == base.caps and p.role == role


def test_get_member_roles_tuples_are_accepted_as_roles():
    p = mp.compute_effective_policy(None, None, [(ROLE_ADMIN, "p1"), (ROLE_VIEWER, "p2")])
    assert p.caps == frozenset() and p.role == ROLE_VIEWER


def test_write_project_off_forces_the_other_four_off_even_over_an_override():
    p = mp.compute_effective_policy({"write_project": False},
                                    {"create_subproject": True, "delete_project": True},
                                    [ROLE_ADMIN])
    assert p.caps == frozenset()
    assert p.source(W) == mp.SOURCE_DEFAULT
    for cid in (C, DS, DP, WL):
        assert p.source(cid) == mp.SOURCE_IMPLICATION
        assert p.reasons[cid] == mp.REASON_WRITE_PROJECT_OFF
    # the same through a person override on top of a permissive default
    p = mp.compute_effective_policy({cid: True for cid in ALL}, {"write_project": False}, [ROLE_MEMBER])
    assert p.caps == frozenset() and p.source(W) == mp.SOURCE_OVERRIDE


def test_role_clamp_wins_over_the_implication_for_the_reported_source():
    p = mp.compute_effective_policy({"write_project": False}, None, [ROLE_VIEWER])
    assert set(p.sources.values()) == {mp.SOURCE_ROLE_CLAMP}


def test_policy_helpers():
    p = mp.compute_effective_policy({"delete_subproject": True}, None, [ROLE_MEMBER])
    assert p.allows(DS) and not p.allows("not_a_capability")
    assert list(p.as_dict()) == list(ALL)
    readout = p.readout()
    assert readout[DS] == {"allowed": True, "source": "default", "reason": None}
    assert readout[DP] == {"allowed": False, "source": "builtin", "reason": None}
    assert p.source("not_a_capability") == mp.SOURCE_BUILTIN


# ---------------------------------------------------------------------------
# the scope carrier and the plain-list rule
# ---------------------------------------------------------------------------

def test_carrier_is_a_list_in_every_way_a_consumer_uses_one():
    s = mp.make_scope(["a", "b", "c"])
    assert isinstance(s, list) and isinstance(s, mp.ScopedProjectIds)
    assert s == ["a", "b", "c"] and ["a", "b", "c"] == s
    assert "b" in s and "z" not in s
    assert set(s) == {"a", "b", "c"} and frozenset(s) == {"a", "b", "c"}
    assert sorted(s) == ["a", "b", "c"] and len(s) == 3
    assert [p for p in s if p != "b"] == ["a", "c"]
    assert s[0] == "a"


def test_carrier_attributes_survive_copy_but_not_list_slice_or_sorted():
    s = mp.make_scope(["a", "b"], policy={"delete_subproject": True}, owner_email="o@example.com")
    c = copy.copy(s)
    assert isinstance(c, mp.ScopedProjectIds) and c.policy == s.policy and c.owner_email == "o@example.com"
    for degraded in (list(s), s[:], s[:1], sorted(s), s + ["x"]):
        assert type(degraded) is list


def test_make_scope_defaults_and_attributes():
    s = mp.make_scope(["a", "a", "b"])
    assert list(s) == ["a", "b"] and s.explicit == ("a", "b") and s.parent_of == {}
    assert s.policy == mp.BUILTIN_POLICY and s.role == ROLE_MEMBER
    assert s.owner_email == "owner@example.com" and s.workspace_tenant_id

    child = mp.make_scope(["p", "c"], explicit=["p"], parent_of={"c": "p"})
    assert child.explicit == ("p",) and child.parent_of == {"c": "p"}


def test_make_scope_policy_dict_goes_through_the_real_resolution():
    assert mp.make_scope(["a"], policy={"delete_subproject": True}).policy.allows(DS)
    off = mp.make_scope(["a"], policy={"write_project": False}).policy
    assert off.caps == frozenset() and off.source(C) == mp.SOURCE_IMPLICATION
    assert mp.make_scope(["a"], role=ROLE_VIEWER).policy.caps == frozenset()
    explicit_policy = mp.compute_effective_policy({"delete_project": True}, None, [ROLE_MEMBER])
    assert mp.make_scope(["a"], policy=explicit_policy).policy is explicit_policy


def test_a_plain_list_is_refused_in_hosted_mode_and_means_defaults_otherwise(monkeypatch):
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    with pytest.raises(mp.MemberPolicyUnavailable) as exc:
        mp.policy_of(["a"])
    assert exc.value.status_code == 503 and "policy check unavailable" in str(exc.value)
    with pytest.raises(mp.MemberPolicyUnavailable):
        mp.require_capability(["a"], W)
    with pytest.raises(mp.MemberPolicyUnavailable):
        mp.allows(["a"], W)
    with pytest.raises(mp.MemberPolicyUnavailable):
        mp.require_capabilities(["a"], [W])
    # a degraded copy of a real carrier is just as refused
    with pytest.raises(mp.MemberPolicyUnavailable):
        mp.require_capability(list(mp.make_scope(["a"])), W)
    # a real carrier is fine, and None (not scoped) never consults the policy
    assert mp.allows(mp.make_scope(["a"]), W)
    assert mp.allows(None, DP)

    monkeypatch.setenv("MERIDIAN_HOSTED", "")
    assert mp.policy_of(["a"]) == mp.BUILTIN_POLICY
    assert mp.allows(["a"], C) and not mp.allows(["a"], DP)


def test_require_capability_is_a_no_op_for_an_unscoped_caller():
    for cap in ALL:
        mp.require_capability(None, cap)
    mp.require_capabilities(None, ALL)


def test_require_capability_refuses_with_a_structured_error():
    scope = mp.make_scope(["p"], owner_email="owner@example.com", workspace_tenant_id="ws-1")
    mp.require_capability(scope, W)
    with pytest.raises(mp.MemberPolicyDenied) as exc:
        mp.require_capability(scope, DS, target="p")
    e = exc.value
    assert isinstance(e, ValueError)
    assert (e.capability, e.target, e.owner_email, e.workspace_tenant_id) == (DS, "p", "owner@example.com", "ws-1")
    assert str(e) == "member policy: capability delete_subproject required"
    assert e.status_code == 403 and e.error == "capability_required"


def test_require_capabilities_names_one_missing_capability_in_catalog_order():
    scope = mp.make_scope(["p"])
    with pytest.raises(mp.MemberPolicyDenied) as exc:
        mp.require_capabilities(scope, {WL, DP, W, C})
    assert exc.value.capability == DP  # create/write are allowed; DP precedes WL
    with pytest.raises(mp.MemberPolicyDenied) as exc:
        mp.require_capabilities(scope, {"not_a_capability", W})
    assert exc.value.capability == "not_a_capability"
    mp.require_capabilities(scope, {W, C})


def test_a_viewer_carrier_refuses_write_project_with_its_reason():
    scope = mp.make_scope(["p"], role=ROLE_VIEWER)
    with pytest.raises(mp.MemberPolicyDenied) as exc:
        mp.require_capability(scope, W)
    assert exc.value.reason == mp.REASON_VIEWER_ROLE


# ---------------------------------------------------------------------------
# refusal bodies
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cap, sentence", [
    (W, "Your access to this workspace is read-only. You do not have the Edit project content permission."),
    (C, "You do not have the Create subprojects permission in this workspace."),
    (DS, "You cannot delete, detach, archive or merge away subprojects (permission: Delete subprojects)."),
    (DP, "You cannot delete this project (permission: Delete their project)."),
])
def test_denial_detail_names_only_the_blocking_capability_and_the_owner(cap, sentence):
    e = mp.MemberPolicyDenied(cap, owner_email="owner@x.test")
    assert e.detail == f"member policy: capability {cap} required - {sentence} Ask owner@x.test to enable it."
    body = e.body()
    assert body == {
        "detail": e.detail, "error": "capability_required", "capability": cap,
        "capability_label": mp.CAPABILITY_BY_ID[cap].label, "workspace_owner": "owner@x.test",
    }
    others = [c.label for c in mp.CAPABILITIES if c.id != cap]
    assert not any(label in e.detail for label in others)


def test_workspace_level_denial_and_unknown_owner_wording():
    e = mp.MemberPolicyDenied(WL)
    assert "Change workspace-level things permission" in e.detail
    assert e.detail.endswith("Ask the workspace owner to enable it.")
    assert e.body()["workspace_owner"] is None


def test_denial_can_carry_a_custom_sentence_and_unknown_capability():
    e = mp.MemberPolicyDenied(DS, sentence="Deleting X would also remove its 2 subprojects.", owner_email="o@x.test")
    assert "Deleting X would also remove its 2 subprojects. Ask o@x.test" in e.detail
    odd = mp.MemberPolicyDenied("mystery")
    assert odd.label == "mystery" and "member policy: capability mystery required" in odd.detail


def test_scope_denied_matches_the_scope_wording_of_both_transports():
    e = mp.ProjectScopeDenied()
    assert str(e) == "project is outside your access scope"
    assert e.detail == "Project is outside your access scope." and e.status_code == 403
    assert isinstance(e, ValueError)


def test_orphaning_message():
    assert mp.orphaning_message("alpha", 1) == (
        "member policy: deleting alpha would orphan 1 subproject; include them in the same request")
    assert "orphan 3 subprojects" in mp.orphaning_message("alpha", 3)


# ---------------------------------------------------------------------------
# decision functions: classify and required capabilities
# ---------------------------------------------------------------------------

def _row(pid, parent=None, *, parent_exists=None, status="active", children=()):
    row = {"id": pid, "parent_project_id": parent, "status": status, "child_ids": list(children)}
    if parent_exists is not None:
        row["parent_exists"] = parent_exists
    return row


TOP = _row("t")
SUB = _row("s", "t", parent_exists=True)
ORPHAN = _row("o", "gone", parent_exists=False)
PARENT = _row("t", children=["s"])


def test_classify_target():
    assert mp.classify_target(TOP) == "top"
    assert mp.classify_target(_row("x", "")) == "top"
    assert mp.classify_target(SUB) == "subproject"
    assert mp.classify_target(ORPHAN) == "orphan"


def test_classify_target_unknown_parent_existence_is_the_stricter_orphan():
    assert mp.classify_target(_row("x", "p")) == "orphan"
    assert mp.classify_target(_row("x", "p"), {"x": {"parent_exists": True}}) == "subproject"
    assert mp.classify_target(_row("x", "p"), {"x": {"parent_exists": False}}) == "orphan"


def test_classify_target_reads_a_row_object_with_item_access():
    class Row:
        def __init__(self, **kw):
            self.kw = kw

        def __getitem__(self, key):
            return self.kw[key]

    assert mp.classify_target(Row(id="a", parent_project_id="p", parent_exists=1)) == "subproject"
    assert mp.classify_target(Row(id="a", parent_project_id=None)) == "top"


def test_required_caps_for_delete_by_class():
    assert mp.required_caps_for_delete([TOP]) == {W, DP}
    assert mp.required_caps_for_delete([SUB]) == {W, DS}
    assert mp.required_caps_for_delete([ORPHAN]) == {W, DP, DS}
    assert mp.required_caps_for_delete(TOP) == {W, DP}  # a single mapping is a batch of one
    assert mp.required_caps_for_delete([]) == {W}
    assert mp.required_caps_for_delete(None) == {W}


def test_required_caps_for_delete_of_a_parent_with_subprojects_needs_both():
    assert mp.required_caps_for_delete([PARENT]) == {W, DP, DS}
    family = {"t": {"parent_project_id": None, "child_ids": ["s"]}}
    assert mp.required_caps_for_delete([TOP], family) == {W, DP, DS}
    assert mp.required_caps_for_delete([TOP], {"t": {"child_ids": []}}) == {W, DP}


def test_required_caps_for_delete_batch_is_the_union_over_every_row():
    assert mp.required_caps_for_delete([TOP, SUB]) == {W, DP, DS}
    assert mp.required_caps_for_delete([SUB, _row("s2", "t", parent_exists=True)]) == {W, DS}


def test_children_left_behind():
    family = {"t": {"child_ids": ["s1", "s2"]}, "s1": {"child_ids": []}, "u": {"child_ids": []}}
    assert mp.children_left_behind(["t"], family) == {"t": ["s1", "s2"]}
    assert mp.children_left_behind(["t", "s1"], family) == {"t": ["s2"]}
    assert mp.children_left_behind(["t", "s1", "s2"], family) == {}
    assert mp.children_left_behind(["u", "unknown"], family) == {}


def test_required_caps_for_status():
    assert mp.required_caps_for_status(TOP, "archived") == {W, DP}
    assert mp.required_caps_for_status(SUB, "archived") == {W, DS}
    assert mp.required_caps_for_status(ORPHAN, "archived") == {W, DP, DS}
    # restore is symmetric with archive
    assert mp.required_caps_for_status(_row("t", status="archived"), "active") == {W, DP}
    assert mp.required_caps_for_status(
        _row("s", "t", parent_exists=True, status="archived"), "parked") == {W, DS}
    # park / unpark / no status change are plain writes
    assert mp.required_caps_for_status(TOP, "parked") == {W}
    assert mp.required_caps_for_status(_row("t", status="parked"), "active") == {W}
    assert mp.required_caps_for_status(_row("t", status="archived"), "archived") == {W}
    assert mp.required_caps_for_status(TOP, None) == {W}
    # a parent with subprojects needs both even to archive it
    assert mp.required_caps_for_status(PARENT, "archived") == {W, DP, DS}


@pytest.mark.parametrize("garbage", [5, ["archived"], {"s": 1}, True])
def test_required_caps_for_status_unparsable_takes_the_most_restrictive_reading(garbage):
    assert mp.required_caps_for_status(SUB, garbage) == {W, DS}
    assert mp.required_caps_for_status(TOP, garbage) == {W, DP}


def test_required_caps_for_status_unknown_current_status_is_most_restrictive():
    assert mp.required_caps_for_status({"id": "t", "parent_project_id": None}, "parked") == {W, DP}


def test_status_case_and_spacing_cannot_dodge_the_archive_rule():
    assert mp.required_caps_for_status(TOP, " ARCHIVED ") == {W, DP}


NEW_PARENT = {"id": "p2", "parent_project_id": None}


def test_reparent_nest_move_detach_orphan_and_no_op():
    # nest a top-level project under another: a subproject is born, so create + delete_project
    assert mp.required_caps_for_reparent(TOP, None, NEW_PARENT) == {W, C, DP}
    # move a subproject between parents: create + delete_subproject
    assert mp.required_caps_for_reparent(SUB, _row("t"), NEW_PARENT) == {W, C, DS}
    # detach a subproject: it becomes a top-level project
    assert mp.required_caps_for_reparent(SUB, _row("t"), None) == {W, DS, WL}
    # an orphan needs every lifecycle capability
    assert mp.required_caps_for_reparent(ORPHAN, None, NEW_PARENT) == {W, C, DP, DS}
    assert mp.required_caps_for_reparent(ORPHAN, None, None) == {W, C, DP, DS, WL}
    # same parent, and detaching what is already top-level, are plain writes
    assert mp.required_caps_for_reparent(SUB, _row("t"), {"id": "t"}) == {W}
    assert mp.required_caps_for_reparent(TOP, None, None) == {W}


def test_reparent_derives_parent_existence_from_the_current_parent_row():
    unknown = _row("x", "t")  # no parent_exists on the row
    assert mp.required_caps_for_reparent(unknown, _row("t"), NEW_PARENT) == {W, C, DS}
    assert mp.required_caps_for_reparent(unknown, None, NEW_PARENT) == {W, C, DP, DS}


def test_required_caps_for_merge_follows_the_source_class_only():
    assert mp.required_caps_for_merge(TOP, TOP) == {W, DP}
    assert mp.required_caps_for_merge(SUB, TOP) == {W, DS}
    assert mp.required_caps_for_merge(ORPHAN, TOP) == {W, DP, DS}
    assert mp.required_caps_for_merge(PARENT, TOP) == {W, DP, DS}


def test_required_caps_for_create():
    assert mp.required_caps_for_create("parent-id") == {W, C}
    assert mp.required_caps_for_create("") == {W, C}  # an empty string is a subproject attempt
    assert mp.required_caps_for_create(None) == {W, WL}


# ---------------------------------------------------------------------------
# HITL answers
# ---------------------------------------------------------------------------

def _hitl(kind, project_id="p", payload=None):
    return {"id": "h1", "project_id": project_id, "kind": kind,
            "payload": json.dumps(payload) if payload is not None else None}


async def test_hitl_check_is_a_no_op_for_an_unscoped_caller():
    await mp.check_hitl_answer(None, object(), _hitl("manual_issue_screening_toggle"), "yes")
    await mp.check_hitl_answer(None, object(), None, "yes")


async def test_hitl_plain_question_needs_write_project_and_an_in_scope_project():
    scope = mp.make_scope(["p"])
    await mp.check_hitl_answer(scope, None, _hitl("question"), "yes")
    with pytest.raises(mp.ProjectScopeDenied):
        await mp.check_hitl_answer(scope, None, _hitl("question", project_id="other"), "yes")
    with pytest.raises(mp.ProjectScopeDenied):
        await mp.check_hitl_answer(scope, None, None, "yes")
    off = mp.make_scope(["p"], policy={"write_project": False})
    with pytest.raises(mp.MemberPolicyDenied) as exc:
        await mp.check_hitl_answer(off, None, _hitl("question"), "yes")
    assert exc.value.capability == W
    # scope is checked before the capability, so a foreign id keeps the scope refusal
    with pytest.raises(mp.ProjectScopeDenied):
        await mp.check_hitl_answer(off, None, _hitl("question", project_id="other"), "yes")


async def test_hitl_manual_issue_screening_toggle_needs_workspace_level():
    row = _hitl("manual_issue_screening_toggle", payload={"tenant_id": "t"})
    with pytest.raises(mp.MemberPolicyDenied) as exc:
        await mp.check_hitl_answer(mp.make_scope(["p"]), None, row, "yes")
    assert exc.value.capability == WL
    await mp.check_hitl_answer(mp.make_scope(["p"], policy={WL: True}), None, row, "yes")
    # a dismissal has no side effect
    await mp.check_hitl_answer(mp.make_scope(["p"]), None, row, None)


async def test_hitl_hook_project_select_refuses_a_chosen_project_outside_scope():
    payload = {"hostname": "box", "projects": [
        {"id": "p", "name": "mine"}, {"id": "other", "name": "theirs"}, {"name": "no-id"}]}
    row = _hitl("hook_project_select", payload=payload)
    scope = mp.make_scope(["p"])
    await mp.check_hitl_answer(scope, None, row, "mine")
    await mp.check_hitl_answer(scope, None, row, "nobody")  # names no project: no side effect
    for answer in ("theirs", "no-id"):
        with pytest.raises(mp.ProjectScopeDenied):
            await mp.check_hitl_answer(scope, None, row, answer)
    # a payload that does not parse has no side effect either
    await mp.check_hitl_answer(scope, None, {**row, "payload": "{not json"}, "theirs")


async def test_hitl_proposal_github_issue_refuses_a_payload_project_outside_scope():
    scope = mp.make_scope(["p"])
    ok = _hitl("proposal_github_issue", payload={"project_id": "p", "proposal_id": "x"})
    bad = _hitl("proposal_github_issue", payload={"project_id": "other", "proposal_id": "x"})
    await mp.check_hitl_answer(scope, None, ok, "yes")
    with pytest.raises(mp.ProjectScopeDenied):
        await mp.check_hitl_answer(scope, None, bad, "yes")
    with pytest.raises(mp.ProjectScopeDenied):
        await mp.check_hitl_answer(scope, None, {**bad, "payload": json.loads(bad["payload"])}, "yes")


async def test_hitl_check_reads_the_row_by_id(monkeypatch):
    calls = []

    async def fake_get(db, request_id):
        calls.append((db, request_id))
        return _hitl("question") if request_id == "h1" else None

    monkeypatch.setattr(db_module, "get_hitl_request", fake_get)
    scope = mp.make_scope(["p"])
    await mp.check_hitl_answer(scope, "the-db", "h1", "yes")
    assert calls == [("the-db", "h1")]
    with pytest.raises(mp.ProjectScopeDenied):
        await mp.check_hitl_answer(scope, "the-db", "missing", "yes")


async def test_hitl_check_in_hosted_mode_refuses_a_plain_list(monkeypatch):
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    with pytest.raises(mp.MemberPolicyUnavailable):
        await mp.check_hitl_answer(["p"], None, _hitl("question"), "yes")
