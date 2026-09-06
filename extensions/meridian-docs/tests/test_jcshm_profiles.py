from __future__ import annotations

from meridian_docs.document_profile import (
    JCSHM_DOCUMENT_PROFILE,
    normalize_document_profile,
    profile_digest,
)
from meridian_docs.notation_rules import JCSHM_NOTATION_RULES, normalize_notation_rules


def test_jcshm_document_profile_is_deterministic_and_excludes_release_rights():
    profile = normalize_document_profile(JCSHM_DOCUMENT_PROFILE)

    assert profile["version"] == "jcshm-1"
    assert profile["equations"]["representation"] == "native_omml"
    assert profile["equations"]["numbering"] == "section"
    assert profile["style"]["require_explicit_operator_roles"] is True
    assert profile_digest(profile) == profile_digest(profile)


def test_jcshm_notation_rules_are_a_normal_rule_pack():
    rules = normalize_notation_rules(JCSHM_NOTATION_RULES)

    assert rules["version"] == "jcshm-1"
    assert rules["case_mismatch"] == "error"
    assert rules["alias_used"] == "warning"
